"""Validated configuration (architecture section 12).

Two layers of checking:

* Field validators enforce types and sane ranges whenever `Settings` is constructed.
* `Settings.validate_for_startup()` enforces *readiness* rules (real token, configured access,
  private-chat-only, safe size ceilings). The process entry point calls it and refuses to start
  on `ConfigError`. Keeping it separate lets tests build partial settings.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Annotated, Literal

from pydantic import Field, SecretStr, field_validator
from pydantic_settings import BaseSettings, NoDecode, SettingsConfigDict

from app.core.contracts import DeliveryMode
from app.core.errors import ConfigError

# Telegram's documented hosted-API ceilings. Application thresholds must stay below them.
TELEGRAM_HOSTED_PHOTO_CEILING = 10_000_000
TELEGRAM_HOSTED_FILE_CEILING = 50_000_000
TELEGRAM_LOCAL_FILE_CEILING = 2_000_000_000

_TOKEN_RE = re.compile(r"^\d{5,}:[A-Za-z0-9_-]{30,}$")
_PLACEHOLDERS = frozenset(
    {"", "replace_me", "replace-me", "changeme", "change_me", "your_token_here", "<token>", "todo"}
)


def _csv_ints(value: object) -> list[int]:
    if value is None or value == "":
        return []
    if isinstance(value, int):
        return [value]
    if isinstance(value, list | tuple | set):
        items = list(value)
    elif isinstance(value, str):
        items = [p.strip() for p in value.split(",") if p.strip()]
    else:
        raise ValueError("expected a comma-separated list of numeric Telegram user IDs")
    out: list[int] = []
    for raw in items:
        try:
            n = int(raw)
        except (TypeError, ValueError) as exc:
            raise ValueError(f"non-numeric user id in allowlist: {raw!r}") from exc
        if n <= 0:
            # Negative ids are groups/channels; 0 is invalid. Only individual users are allowed.
            raise ValueError(f"allowlist entries must be positive user ids, got {n}")
        out.append(n)
    return list(dict.fromkeys(out))  # dedupe, keep order


def _csv_strs(value: object) -> list[str]:
    if value is None or value == "":
        return []
    if isinstance(value, str):
        return [p.strip() for p in value.split(",") if p.strip()]
    if isinstance(value, list | tuple | set):
        return [str(p).strip() for p in value if str(p).strip()]
    raise ValueError("expected a comma-separated list")


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
    )

    # --- environment / identity ------------------------------------------------------
    app_env: Literal["development", "production"] = "development"
    bot_token: SecretStr = SecretStr("")
    bot_token_file: Path | None = None
    allowed_user_ids: Annotated[list[int], NoDecode] = Field(default_factory=list)
    public_access: bool = False
    private_chat_only: bool = True

    # --- Telegram API profile --------------------------------------------------------
    bot_api_mode: Literal["hosted", "local"] = "hosted"
    local_bot_api_url: str = "http://127.0.0.1:8081"

    # --- delivery / queue ------------------------------------------------------------
    default_delivery_mode: DeliveryMode = DeliveryMode.MEDIA
    max_concurrent_jobs: Annotated[int, Field(ge=1, le=8)] = 2
    max_active_jobs_per_user: Annotated[int, Field(ge=1, le=4)] = 1
    max_queue_size: Annotated[int, Field(ge=1, le=100)] = 10
    max_items_per_job: Annotated[int, Field(ge=1, le=100)] = 30
    extraction_timeout_seconds: Annotated[int, Field(ge=5, le=900)] = 120
    upload_timeout_seconds: Annotated[int, Field(ge=10, le=7200)] = 600
    max_temp_bytes_per_job: Annotated[int, Field(ge=1_000_000)] = 4_000_000_000
    hosted_photo_max_bytes: Annotated[int, Field(ge=1)] = 9_500_000
    hosted_file_max_bytes: Annotated[int, Field(ge=1)] = 49_000_000
    local_file_max_bytes: Annotated[int, Field(ge=1)] = 1_900_000_000
    native_hevc: bool = False

    # --- retention / cleanup ---------------------------------------------------------
    job_metadata_ttl_hours: Annotated[int, Field(ge=1, le=168)] = 24
    orphan_ttl_minutes: Annotated[int, Field(ge=1, le=1440)] = 30
    min_free_disk_bytes: Annotated[int, Field(ge=0)] = 1_000_000_000

    # --- retries / throttling --------------------------------------------------------
    retry_max_attempts: Annotated[int, Field(ge=0, le=5)] = 2
    retry_base_delay_seconds: Annotated[float, Field(gt=0, le=60)] = 2.0
    retry_max_delay_seconds: Annotated[float, Field(gt=0, le=600)] = 60.0
    status_edit_min_interval_seconds: Annotated[float, Field(ge=0, le=60)] = 2.0

    # --- paths -----------------------------------------------------------------------
    data_dir: Path = Path("data")
    work_dir: Path = Path("tmp/jobs")
    instagram_cookies_file: Path | None = None

    # --- extractor worker boundary ---------------------------------------------------
    worker_mode: Literal["inprocess", "remote"] = "inprocess"
    worker_url: str = "http://127.0.0.1:8090"
    worker_secret: SecretStr = SecretStr("")
    worker_secret_file: Path | None = None
    worker_host: str = "127.0.0.1"
    worker_port: Annotated[int, Field(ge=1, le=65535)] = 8090
    egress_proxy_url: str | None = None
    extra_allowed_hosts: Annotated[list[str], NoDecode] = Field(default_factory=list)

    # --- operations ------------------------------------------------------------------
    log_level: str = "INFO"
    polling_timeout_seconds: Annotated[int, Field(ge=1, le=50)] = 10
    heartbeat_interval_seconds: Annotated[int, Field(ge=1, le=300)] = 15
    heartbeat_max_age_seconds: Annotated[int, Field(ge=5, le=3600)] = 90
    alerts_enabled: bool = False
    alert_failure_threshold: Annotated[int, Field(ge=2, le=50)] = 5

    # ------------------------------------------------------------------------------------
    @field_validator("allowed_user_ids", mode="before")
    @classmethod
    def _parse_allowlist(cls, v: object) -> list[int]:
        return _csv_ints(v)

    @field_validator("extra_allowed_hosts", mode="before")
    @classmethod
    def _parse_hosts(cls, v: object) -> list[str]:
        return _csv_strs(v)

    @field_validator("log_level")
    @classmethod
    def _log_level(cls, v: str) -> str:
        v = v.upper()
        if v not in {"DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"}:
            raise ValueError("LOG_LEVEL must be DEBUG, INFO, WARNING, ERROR or CRITICAL")
        return v

    # --- derived values --------------------------------------------------------------
    @property
    def db_path(self) -> Path:
        return self.data_dir / "bot.sqlite3"

    @property
    def heartbeat_file(self) -> Path:
        return self.data_dir / "heartbeat"

    @property
    def file_cap_bytes(self) -> int:
        """Largest single file we will upload under the active API profile."""
        return self.local_file_max_bytes if self.bot_api_mode == "local" else self.hosted_file_max_bytes

    @property
    def photo_cap_bytes(self) -> int:
        """Native photo ceiling. Telegram applies it on the local profile too."""
        return self.hosted_photo_max_bytes

    def resolved_token(self) -> str:
        if self.bot_token_file is not None:
            try:
                return self.bot_token_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ConfigError(f"cannot read BOT_TOKEN_FILE: {exc.strerror}") from exc
        return self.bot_token.get_secret_value().strip()

    def resolved_worker_secret(self) -> str:
        if self.worker_secret_file is not None:
            try:
                return self.worker_secret_file.read_text(encoding="utf-8").strip()
            except OSError as exc:
                raise ConfigError(f"cannot read WORKER_SECRET_FILE: {exc.strerror}") from exc
        return self.worker_secret.get_secret_value().strip()

    # --- startup readiness -----------------------------------------------------------
    def validate_for_startup(self) -> None:
        """Raise `ConfigError` listing every reason the process must not start."""
        problems: list[str] = []
        prod = self.app_env == "production"

        try:
            token = self.resolved_token()
        except ConfigError as exc:
            token = ""
            problems.append(str(exc))
        if token.lower() in _PLACEHOLDERS:
            problems.append("BOT_TOKEN is missing or still a placeholder")
        elif not _TOKEN_RE.match(token):
            problems.append("BOT_TOKEN does not look like a Telegram bot token")

        if prod and not self.public_access and not self.allowed_user_ids:
            problems.append("Production requires at least one numeric ALLOWED_USER_IDS entry or PUBLIC_ACCESS=true")
        if (prod or self.public_access) and not self.private_chat_only:
            problems.append("PRIVATE_CHAT_ONLY must be true in production and whenever public access is enabled")

        if self.hosted_photo_max_bytes > TELEGRAM_HOSTED_PHOTO_CEILING:
            problems.append("HOSTED_PHOTO_MAX_BYTES exceeds Telegram's 10 MB photo limit")
        if self.hosted_file_max_bytes > TELEGRAM_HOSTED_FILE_CEILING:
            problems.append("HOSTED_FILE_MAX_BYTES exceeds Telegram's hosted 50 MB limit")
        if self.local_file_max_bytes > TELEGRAM_LOCAL_FILE_CEILING:
            problems.append("LOCAL_FILE_MAX_BYTES exceeds the local Bot API 2000 MB limit")

        if self.bot_api_mode == "local" and not self.local_bot_api_url.startswith(("http://", "https://")):
            problems.append("LOCAL_BOT_API_URL must be an http(s) URL when BOT_API_MODE=local")

        if self.worker_mode == "remote":
            if not self.worker_url.startswith(("http://", "https://")):
                problems.append("WORKER_URL must be an http(s) URL when WORKER_MODE=remote")
            try:
                secret = self.resolved_worker_secret()
            except ConfigError as exc:
                secret = ""
                problems.append(str(exc))
            if len(secret) < 24 or secret.lower() in _PLACEHOLDERS:
                problems.append("WORKER_SECRET(_FILE) must be a random value of at least 24 characters")

        if problems:
            raise ConfigError("; ".join(problems))
