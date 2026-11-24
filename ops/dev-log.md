# Development log

Chronological record of commands that were actually run and what actually happened (tasks.md
section 1: "record commands and outcomes in the development log"). Entries are facts, not
intentions. `PASS` / `FAIL` / `NOT RUN (reason)`.

Test command used throughout: `uv run pytest -q` (default run excludes `live` and `docker` markers).

---

## Phase 0 - setup and foundation (2026-11-24)

### T00.2 environment verification

| Check | Command | Result |
| --- | --- | --- |
| Python | `python --version` / `py -0p` | system default 3.11.17, also 3.14.3. Project is pinned to **3.12** (architecture: 3.12+) |
| Python 3.12 | `uv python install 3.12` | PASS - CPython 3.12.15 installed |
| Git | `git --version` | PASS - 2.55.0.windows.1 |
| FFmpeg / FFprobe | `ffmpeg -version` | PASS - 9.0.2 (gyan.dev full build; has libx264, libx265, libvpx-vp9, libopus) |
| uv | `uv --version` | PASS - 0.13.1 |
| Docker client / Compose | `docker --version`, `docker compose version` | PASS - 29.6.1 / v5.2.0 |
| **Docker daemon** | `docker info` | **FAIL - not running** (`open //./pipe/dockerDesktopLinuxEngine: The system cannot find the file specified`). Containers could not be built or run at this point. |
| Network / PyPI | `uv pip install --dry-run ...` | PASS - dependencies resolved |

Resolved dependency set (see `uv.lock`): aiogram 3.34.0, yt-dlp 2026.11.23, gallery-dl 1.32.21,
pydantic 2.14.4, pydantic-settings 2.16.3, aiohttp 3.14.5, pytest 9.2.0, pytest-asyncio 1.5.0, ruff 0.17.6.

### T00.1 / T00.3 repository and dependencies

* `git init -b main`; `.gitignore` and `.gitattributes` written **before** the first `git add`.
* `uv lock`, `uv sync` -> PASS. `uv export --format requirements-txt --no-dev --no-emit-project --hashes -o requirements.lock` -> PASS (hashed).
* Local git config: `core.hooksPath=.githooks` (repo-local only). Global git identity was left untouched.

### T00.5 configuration

* `app/config.py`, `.env.example`. `tests/unit/test_config.py`: PASS (37 tests). A completeness test
  initially FAILED because `WORKER_HOST` / `WORKER_PORT` were missing from `.env.example`; fixed.

### T00.7 test infrastructure and secret scanning

* `scripts/scan_secrets.py` + `.githooks/pre-commit`. The real-repo scan initially FAILED on my own
  test file (a literal credentials-in-URL sample) - the scanner was right; the sample is now built at runtime.
* `tests/conftest.py` generates synthetic media with FFmpeg lavfi (H.264/AAC mp4+mkv, VP9/Opus webm,
  HEVC, split streams, JPEG/PNG, a deliberately broken JPEG). Nothing binary is committed.

### Validation of the tests themselves

* Mutation check: replaced the exact host lookup in `url_validator.py` with `host.endswith(...)`;
  3 spoofing tests failed as intended; original restored (`74 passed`).

---

## Phase 1 - extraction feasibility, OFFLINE slice only (2026-11-24)

> **No platform was contacted and no live test was run.** The live part (T01.1-T01.6, T01.8, T01.9)
> needs permission-cleared URLs (T00.6), a bot token (T00.4) and/or API credentials, which only the owner has.

* **Installed-source reading** (gallery_dl 1.32.21, yt_dlp 2026.11.23): findings recorded in `ops/feasibility.md` section 3.
* **Real-binary flag checks against a loopback server** (`gallery-dl --config-ignore -j`, `-D/-f`,
  `yt-dlp --use-extractors ...`): `-j` output shape confirmed; `directlink` has no `num` (`{num}` -> `None`);
  unsupported URL -> exit 64; `--use-extractors "tiktok,instagram,instagram:story,twitter"` selects the
  right extractor for platform URLs and rejects `https://example.com/video.mp4` (generic excluded). PASS.
* **Offline routing** (`scripts/feasibility_probe.py offline`): every PRD URL shape is claimed by an
  extractor; **TikTok `/photo/<id>` is claimed only by gallery-dl** (yt-dlp falls through to generic).
  IG Highlights, IG Stories without an id, and profile URLs also match extractors, so the app's own
  validator must (and does) reject them. Pattern match only.
* **Bug found by tests:** `ffprobe` rejects `-nostdin` (ffmpeg-only option) -> every probe returned
  "unprobeable". Fixed (`stdin=DEVNULL`); rule for later: `-nostdin` for ffmpeg only.
* `tests/unit/test_cli_flags.py`: every CLI option the runners will use exists in the installed versions. PASS.
* `tests/unit/test_feasibility_probe.py`: parsers validated against real tool output. PASS.
* Full unit run at end of this slice: `218 passed` (+6 dependency-pin tests = 224).

### Not done in Phase 1 (owner-dependent) - see ops/feasibility.md section 6

T01.1-T01.6 (live extraction), T01.8 (hosted Telegram delivery), T01.9 (local Bot API), and therefore
T01.10 (matrix still entirely "Not tested").
