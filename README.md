# Telegram Social Media Downloader Bot

A self-hosted Telegram bot for TikTok, Instagram and X/Twitter that runs **natively** on **Windows** and **Linux VPS**
(no Docker required). Paste **one** post URL in a private chat to receive the photos and videos in source order with the
original link and an *Original files* button for byte-exact delivery. It never re-encodes, resizes or substitutes media:
if Telegram cannot play a file natively it is sent unchanged as a document, and a file that is too large is reported,
not shrunk.

Specifications: [PRD](PRD.md) · [Architecture](architecture.md) · [Task plan](tasks.md) · [Dev log](ops/dev-log.md)

## Status - read this first

| Area | State |
| --- | --- |
| Code for all P0 features (Phases 0-6) | Implemented. |
| Automated tests | Offline suite passes on Windows (`uv run pytest`) and Linux POSIX checks (`ops/dev-log.md`); tests use **synthetic** fixtures, a **fake Telegram** server and **loopback** media. No real platform or real Telegram was contacted. |
| Native deployment | Windows launcher (`start-bot.bat`) and Ubuntu/Debian `systemd` bootstrap (`ops/setup-ubuntu.sh`) implemented. Not yet run on a live VPS. |
| Live platform behaviour | **Unverified.** Whether the pinned extractors still return what the adapters expect on today's TikTok/Instagram/X, the CDN allowlist, Story access, and Telegram's real limits are listed as assumptions A1-A9 in [ops/feasibility.md](ops/feasibility.md). |
| Telegram clients | **Unverified** on Android and iOS (rendering, playback, clickable links). |
| Acceptance | **Not accepted.** See [ops/test-report.md](ops/test-report.md) for the status of every acceptance test (AT01-AT26) and [tasks.md](tasks.md) for what remains. |

Nothing here should be described as "live-tested" until the owner steps in [Verifying it for real](#verifying-it-for-real-owner-only-live-acceptance)
have been run and recorded.

## What it supports

| Input | Delivered | Notes |
| --- | --- | --- |
| TikTok video, `vm.`/`vt.tiktok.com` short links | Video (+audio) | Highest-resolution source the extractor offers; separate tracks are merged by stream copy only. |
| TikTok photo-mode post | All slides in order (audio track not sent) | Fewer items than the source declares is reported as *partial*. |
| Instagram single photo / Reel / carousel (public) | Photos and videos in source order | Anonymous access only. A login wall is reported, never bypassed. |
| Instagram Story (one URL = one Story) | That Story's photo or video | Only after proving it is public (account not private, no close-friends/subscriber marker) and only with an owner-authorised session you provide. Fails closed. |
| X/Twitter post (photos, video, GIF, mixed) | The post's own attachments, in order | Quoted-post and retweet media are rejected; no session is ever used. |

Not supported, by design: private or restricted content, profiles/highlights/lives/ads, anything that needs
defeating a login, CAPTCHA or rate limit, more than one link per message, group chats, transcoding.

## Prerequisites

* **[uv](https://docs.astral.sh/uv/)** — manages Python 3.12 and the locked `.venv` automatically (`yt-dlp` and `gallery-dl` are pinned inside `.venv`).
* **FFmpeg and FFprobe** on `PATH`:
  * Windows: `winget install Gyan.FFmpeg` (then open a new terminal so `PATH` updates)
  * Ubuntu/Debian: `sudo apt-get install -y ffmpeg` (installed automatically by `ops/setup-ubuntu.sh`)
* **Git** (for cloning and updates)

No Docker, Chromium, Node.js, or external database server is needed.

## Quick start — Windows

1. Clone the repository and open its folder.
2. Copy `.env.example` to `.env` (`copy .env.example .env` or `Copy-Item .env.example .env`), then set `BOT_TOKEN`
   (from `@BotFather`) and either `ALLOWED_USER_IDS` (comma-separated numeric Telegram user IDs) or `PUBLIC_ACCESS=true`.
3. Start the bot with the native launcher:

```bat
start-bot.bat
```

`start-bot.bat` checks `.env`, `ffmpeg`, and `ffprobe`, creates `.venv` with `uv sync --locked --no-dev` if missing
(pass `start-bot.bat --sync` to re-sync after updates), ensures `data/` and `tmp\jobs\` exist, validates configuration,
and runs `.venv\Scripts\python.exe -m app.main`. Press `Ctrl+C` to stop cleanly.

Or run directly from PowerShell (recommended for development and running tests):

```powershell
uv sync
git config core.hooksPath .githooks   # pre-commit secret scanner (refuses tokens, cookies, media)
Copy-Item .env.example .env           # set BOT_TOKEN and ALLOWED_USER_IDS (or PUBLIC_ACCESS=true)
uv run python -m app.main
```

## Production deployment — Linux VPS (Ubuntu/Debian + `systemd`)

Run the idempotent setup script on an Ubuntu/Debian VPS:

```bash
sudo mkdir -p /opt/tmd && sudo chown "$USER:$USER" /opt/tmd
git clone <your repository url> /opt/tmd && cd /opt/tmd
sudo bash ops/setup-ubuntu.sh
```

What `ops/setup-ubuntu.sh` does:

* Installs `ffmpeg`, `git`, `curl`, `ca-certificates`, and `uv` (if missing).
* Runs under your `SUDO_USER`, or creates a dedicated unprivileged `tmd` system user when invoked directly as `root`.
* Syncs Python 3.12 into `.venv` using `uv sync --locked --no-dev`.
* Creates `.env` (`0600`) from `.env.example` without overwriting existing user edits, sets `APP_ENV=production` and
  `WORKER_MODE=inprocess`, and prompts (hidden) for `BOT_TOKEN` and access settings if still placeholders.
* Pre-creates `data/`, `tmp/jobs/`, and `secrets/` (`0700`) with proper ownership and validates startup config.
* Installs, enables, and starts `/etc/systemd/system/tmd-bot.service` with native `systemd` hardening
  (`NoNewPrivileges=true`, `PrivateTmp=true`, `ProtectSystem=strict`, `ReadWritePaths=...`, `MemoryMax=2G`, `TasksMax=256`,
  `Restart=on-failure`).

Common service commands on the VPS:

```bash
sudo systemctl status tmd-bot
sudo journalctl -u tmd-bot -f
sudo systemctl restart tmd-bot
sudo -u tmd bash -c 'cd /opt/tmd && .venv/bin/python -m app.healthcheck'   # replace tmd with your SUDO_USER if applicable
```

Full VPS hardening, updates/rollbacks, secrets/cookies permissions, optional native multi-process worker/proxy on
`127.0.0.1`, optional local Telegram Bot API, and Windows operation details: [ops/deploy.md](ops/deploy.md).
When something misbehaves: [ops/troubleshooting.md](ops/troubleshooting.md).

## Configuration & health check

* **Access control:** Default `PUBLIC_ACCESS=false` requires `ALLOWED_USER_IDS` (comma-separated numeric Telegram IDs).
  Set `PUBLIC_ACCESS=true` to allow any Telegram user in their own private chat. When `ALERTS_ENABLED=true`,
  operational alerts go to `ALLOWED_USER_IDS`.
* **Production mode:** `APP_ENV=production` works with the default single-process `WORKER_MODE=inprocess` and enforces
  all production startup guards (valid `BOT_TOKEN`, `PRIVATE_CHAT_ONLY=true`, non-empty `ALLOWED_USER_IDS` unless
  `PUBLIC_ACCESS=true`). If you enable the optional native multi-process `WORKER_MODE=remote`, `APP_ENV=production`
  also requires a strong `WORKER_SECRET` (>= 24 chars) and valid `WORKER_URL` (default `http://127.0.0.1:8090`).
* **File-based secrets:** Any secret can live in a restricted local file (`secrets/bot_token`, `secrets/worker_secret`,
  `secrets/instagram_cookies.txt`) referenced via `BOT_TOKEN_FILE`, `WORKER_SECRET_FILE`, or `INSTAGRAM_COOKIES_FILE`.
* **Health check CLI:** Run `uv run python -m app.healthcheck` (or `.venv/bin/python -m app.healthcheck` /
  `.venv\Scripts\python.exe -m app.healthcheck`). It reads `data/heartbeat` without touching the network and exits `0`
  when healthy or `1` with diagnostics on stderr (polling freshness, DB writability, free disk, worker reachability).

## Using it

Send a supported URL in a private chat with the bot. `/settings` toggles **Media** (default: photos/videos shown
natively, lossless only) and **Original files** (everything as documents, unmodified). `/cancel` stops your
active or queued job. After an album the closing message carries the link and an *Original files* button.
Outcomes are never optimistic: *complete*, *partial* (with the missing positions), *unverified* (the source could
not prove how many items exist) or *delivery unknown* (an upload may or may not have arrived; it is never replayed
automatically, you get a Retry button).

## How it is kept safe

* **Access:** The bot checks the sender and their private chat before starting a job. Callbacks are bound to the
  user, chat and job in both access modes.
* **What:** Exact-host allowlist, one URL per message, strict IP classification (incl. IPv4-in-IPv6), DNS pinned to
  validated addresses, validated redirects, no user text ever reaches a shell or an option position.
* **Where it runs:** Per-job temporary workspace (`tmp/jobs/<32hex>`), symlink and path-escape rejection, byte, disk,
  time, item and queue limits, process-tree termination on timeout or cancel (`taskkill /T /F` on Windows, `killpg` +
  POSIX rlimits on Linux), hardened `systemd` unit on Linux VPS, and optional local multi-process worker + allowlisting
  egress proxy on `127.0.0.1`.
* **Secrets:** Never in logs, Telegram or git (a pre-commit scanner in `.githooks/pre-commit` enforces it); the optional
  Instagram cookies file is copied privately per job inside the throw-away workspace so the original is never modified.
* **Honesty:** No silent downscale, re-encode or thumbnail substitution; counts and omissions are reported.

## Verifying it for real (Owner-Only Live Acceptance)

Everything above was exercised offline. The codebase, native launchers, and automated suites for all P0 requirements
are complete. To establish live production readiness and complete client handover, the following **owner-only live
acceptance items** must be run with your private credentials, physical devices, and VPS infrastructure:

| Task ID | Item | Action / What to Run |
| --- | --- | --- |
| **T00.4** | BotFather token & numeric ID | Generate bot token from `@BotFather`, find numeric ID via `@userinfobot`, add to `.env` (or `secrets/bot_token`) and `ALLOWED_USER_IDS`. |
| **T00.6** | Live test corpus | Copy `tests/live/corpus.example.txt` to `tests/live/corpus.txt` and populate with permission-cleared public URLs for TikTok, Instagram, and X. |
| **T01.1 - T01.6** | Platform live feasibility spike | Run `uv run python scripts/feasibility_probe.py live --download` on real endpoints and record results in [ops/feasibility.md](ops/feasibility.md). |
| **T01.8 - T01.9** | Real Telegram smoke & local Bot API | Run `uv run python scripts/telegram_smoke.py` (hosted API) and `--large` (local Bot API with API ID/hash) to verify real Telegram delivery. |
| **T06.5** | Opt-in live test suite | Run `uv run pytest -m live` with network access and live corpus. |
| **T06.6** | Device UX & playback verification | Verify rendering, album layout, audio sync, and clickable source links on physical Telegram Android and iOS clients. |
| **T06.9 - T06.13** | VPS production deployment & smoke | Follow [ops/deploy.md](ops/deploy.md) to provision VPS, run `sudo bash ops/setup-ubuntu.sh`, verify `systemctl status tmd-bot` and `python -m app.healthcheck`, and verify restart recovery. |
| **Phase 7 (T07.1 - T07.8)** | Client acceptance & final sign-off | Live demonstration of TikTok, Instagram, and X downloading, original-file delivery, error handling, and final repository sign-off. |

Record real-world outcomes in [ops/test-report.md](ops/test-report.md) and [ops/feasibility.md](ops/feasibility.md).

## Handover documentation index

Every aspect of system architecture, deployment, troubleshooting, and verification is documented in full:

* **[PRD.md](PRD.md)** — Functional requirements, access constraints, failure behaviors, and media quality rules.
* **[architecture.md](architecture.md)** — Security invariants, native runtime & optional multi-process topology, egress proxy design, and data flows.
* **[tasks.md](tasks.md)** — Detailed phase-by-phase task tracking from foundation through client acceptance.
* **[ops/deploy.md](ops/deploy.md)** — Native deployment manual for Linux VPS (`ops/setup-ubuntu.sh`, `systemd`, updates/rollbacks, secrets, optional local worker/proxy) and Windows (`start-bot.bat`).
* **[ops/troubleshooting.md](ops/troubleshooting.md)** — Error triage for Windows & Linux VPS: startup/auth, `ffmpeg`/`ffprobe` PATH, `systemd`/`journalctl`, SQLite WAL, extractors, cookies, and disk cleanup.
* **[ops/test-report.md](ops/test-report.md)** — AT01–AT26 acceptance test matrix, offline automated evidence, and live status tracking.
* **[ops/dev-log.md](ops/dev-log.md)** — Chronological engineering log of commands, observed tool outputs, security mutation checks, and bug fixes.
* **[ops/feasibility.md](ops/feasibility.md)** — Extractor feasibility spike findings, CLI flags, format mappings, and assumptions A1–A9.

## Tests

| Layer | Command | What it needs |
| --- | --- | --- |
| Unit, contract, integration, security | `uv run pytest` | FFmpeg/FFprobe; no network |
| Lint & formatting | `uv run ruff check . && uv run ruff format --check app tests scripts` | Dev dependencies (`uv sync`) |
| Secret scan | `uv run python scripts/scan_secrets.py --all` | Python 3.12 |
| Live (opt-in) | `uv run pytest -m live` | Corpus, network, token |

Fixtures are synthetic (`tests/fixtures/make_fixtures.py` regenerates them; a test fails if they drift).

## Updating the extractors (they break; that is normal)

Extractors are pinned exactly in `pyproject.toml`, `uv.lock`, and `requirements.lock`. Update deliberately — procedure in
[ops/deploy.md](ops/deploy.md#updating-the-extractors-safely). Never float on `latest`.

## Layout

```text
app/bot/          Telegram handlers, authorization, keyboards, messages, notifier
app/core/         contracts, state machine, URL validator, net guard, job queue, runner, retries
app/extractors/   adapters (TikTok/Instagram/X), tool runners, optional worker service + client, short-link resolver
app/media/        probe, quality selection, lossless remux, delivery planner
app/services/     subprocess runner, workspaces, Telegram sender, optional egress proxy, alerts, health
app/storage/      SQLite (WAL) and migrations
ops/              setup-ubuntu.sh, deploy, troubleshooting, dev log, feasibility evidence, test report
scripts/          feasibility probe, Telegram smoke test, secret scanner
start-bot.bat     Windows native launcher
tests/            unit, contract, integration, live (opt-in), fixtures
```

## Licences and terms

yt-dlp (Unlicense), aiogram (MIT), pydantic and aiohttp (MIT/Apache-2.0) are used as libraries. **gallery-dl is
GPL-2.0-only and is invoked as a separate subprocess only; it is never imported** by this code. FFmpeg comes from the
system package (`apt` on Ubuntu/Debian or `winget`/official build on Windows). Use the bot only for content you are
permitted to access and respect each platform's terms. It does not circumvent access controls.
