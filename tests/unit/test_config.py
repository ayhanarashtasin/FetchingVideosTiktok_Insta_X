"""T00.5: configuration validation, private-chat-only default and startup allowlist checks."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.config import Settings
from app.core.contracts import DeliveryMode
from app.core.errors import ConfigError
from tests.conftest import fake_bot_token

REPO = Path(__file__).resolve().parents[2]


def settings(**kw):
    return Settings(_env_file=None, **kw)


def test_documented_defaults():
    s = settings()
    assert s.default_delivery_mode is DeliveryMode.MEDIA
    assert s.private_chat_only is True
    assert (s.max_concurrent_jobs, s.max_active_jobs_per_user, s.max_queue_size) == (2, 1, 10)
    assert s.max_items_per_job == 30
    assert s.extraction_timeout_seconds == 120
    assert s.upload_timeout_seconds == 600
    assert s.max_temp_bytes_per_job == 4_000_000_000
    assert (s.hosted_photo_max_bytes, s.hosted_file_max_bytes) == (9_500_000, 49_000_000)
    assert s.local_file_max_bytes == 1_900_000_000
    assert s.job_metadata_ttl_hours == 24
    assert s.orphan_ttl_minutes == 30
    assert s.worker_mode == "inprocess"
    assert s.bot_api_mode == "hosted"
    assert s.allowed_user_ids == []


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("123456789", [123456789]),
        ("1, 2 ,3", [1, 2, 3]),
        ("5,5,6", [5, 6]),
        ("", []),
        ([7, 8], [7, 8]),
    ],
)
def test_allowlist_parsing(raw, expected):
    assert settings(allowed_user_ids=raw).allowed_user_ids == expected


@pytest.mark.parametrize("raw", ["abc", "1,two", "-100123456", "0", "@someone"])
def test_allowlist_rejects_non_numeric_or_non_user_ids(raw):
    with pytest.raises(ValueError):
        settings(allowed_user_ids=raw)


def test_allowlist_from_environment(monkeypatch):
    monkeypatch.setenv("ALLOWED_USER_IDS", "10,20")
    monkeypatch.setenv("MAX_QUEUE_SIZE", "3")
    s = Settings(_env_file=None)
    assert s.allowed_user_ids == [10, 20]
    assert s.max_queue_size == 3


def test_range_validation():
    with pytest.raises(ValueError):
        settings(max_concurrent_jobs=0)
    with pytest.raises(ValueError):
        settings(log_level="LOUD")


def good(**kw):
    base = dict(bot_token=fake_bot_token(), allowed_user_ids=[111])
    base.update(kw)
    return settings(**base)


def test_valid_development_settings_start():
    good().validate_for_startup()


@pytest.mark.parametrize("token", ["", "replace_me", "REPLACE_ME", "changeme", "todo"])
def test_placeholder_or_missing_token_refuses_to_start(token):
    with pytest.raises(ConfigError, match="BOT_TOKEN"):
        good(bot_token=token).validate_for_startup()


def test_malformed_token_refuses_to_start():
    with pytest.raises(ConfigError, match="does not look like"):
        good(bot_token="not-a-token").validate_for_startup()


def test_token_from_file(tmp_path):
    f = tmp_path / "token"
    f.write_text(fake_bot_token() + "\n")
    s = settings(bot_token_file=f, allowed_user_ids=[1])
    assert s.resolved_token() == fake_bot_token()
    s.validate_for_startup()


def test_missing_token_file_is_a_config_error(tmp_path):
    with pytest.raises(ConfigError, match="BOT_TOKEN_FILE"):
        good(bot_token_file=tmp_path / "nope").validate_for_startup()


def test_token_never_appears_in_repr():
    s = good()
    assert fake_bot_token() not in repr(s)
    assert fake_bot_token() not in str(s.model_dump())


def prod(**kw):
    base = dict(
        app_env="production",
        worker_mode="remote",
        worker_secret="s" * 32,
    )
    base.update(kw)
    return good(**base)


def test_production_requires_allowlist():
    with pytest.raises(ConfigError, match="ALLOWED_USER_IDS"):
        prod(allowed_user_ids=[]).validate_for_startup()


def test_production_requires_private_chat_only():
    with pytest.raises(ConfigError, match="PRIVATE_CHAT_ONLY"):
        prod(private_chat_only=False).validate_for_startup()


def test_production_rejects_inprocess_worker():
    with pytest.raises(ConfigError, match="NOT isolated"):
        prod(worker_mode="inprocess").validate_for_startup()


@pytest.mark.parametrize("secret", ["", "short", "replace_me"])
def test_remote_worker_needs_a_real_secret(secret):
    with pytest.raises(ConfigError, match="WORKER_SECRET"):
        prod(worker_secret=secret).validate_for_startup()


def test_production_valid_configuration_starts():
    prod().validate_for_startup()


def test_all_problems_are_reported_together():
    with pytest.raises(ConfigError) as exc:
        prod(bot_token="", allowed_user_ids=[], private_chat_only=False).validate_for_startup()
    msg = str(exc.value)
    assert "BOT_TOKEN" in msg and "ALLOWED_USER_IDS" in msg and "PRIVATE_CHAT_ONLY" in msg


@pytest.mark.parametrize(
    "overrides",
    [
        {"hosted_photo_max_bytes": 10_000_001},
        {"hosted_file_max_bytes": 50_000_001},
        {"local_file_max_bytes": 2_000_000_001},
    ],
)
def test_size_thresholds_must_stay_below_telegram_ceilings(overrides):
    with pytest.raises(ConfigError, match="exceeds"):
        good(**overrides).validate_for_startup()


def test_local_api_profile_caps_and_url():
    s = good(bot_api_mode="local")
    assert s.file_cap_bytes == s.local_file_max_bytes
    assert good().file_cap_bytes == 49_000_000
    assert s.photo_cap_bytes == 9_500_000  # photo limit applies on the local profile too
    with pytest.raises(ConfigError, match="LOCAL_BOT_API_URL"):
        good(bot_api_mode="local", local_bot_api_url="telegram-bot-api:8081").validate_for_startup()


def test_env_example_loads_and_is_not_startable_as_is():
    """The shipped example documents every default, but must never be usable unedited."""
    s = Settings(_env_file=REPO / ".env.example")
    assert s.allowed_user_ids == [123456789]
    assert s.max_queue_size == 10
    with pytest.raises(ConfigError, match="BOT_TOKEN"):
        s.validate_for_startup()


def test_env_example_documents_every_setting():
    text = (REPO / ".env.example").read_text(encoding="utf-8").lower()
    skip = {"bot_token_file", "worker_secret_file"}  # documented in comments as alternatives
    missing = [name for name in Settings.model_fields if name not in skip and name not in text]
    assert not missing, f".env.example is missing: {missing}"
