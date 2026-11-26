# Deployment guide

Native Python 3.12 deployment without Docker. Primary production target: one **Linux VPS** (Ubuntu/Debian with
`systemd`). Starting sizing from the PRD/architecture: **2 vCPU, 4 GB RAM, 40 GB SSD** (measure with your real
workload). No inbound port is needed: the bot uses Telegram long polling. For running locally on **Windows**, see
[Section 10: Native Windows operation](#10-native-windows-operation).

> Status: the native `systemd` bootstrap (`ops/setup-ubuntu.sh`) and Windows launcher (`start-bot.bat`) have been
> verified offline, **not on a live VPS**. Treat sections 1-11 as a checklist to validate on your host (T06.9-T06.13).

## 1. Linux VPS host preparation

**Pick the location for speed first.**
* The bot uploads every fresh video to Telegram, so that upload, not CPU, sets the send-to-delivered time.
* `api.telegram.org` appears to be served from Amsterdam (inferred from its IP and round-trip time), so a host in or near
  the Netherlands or Germany is a sensible first candidate.
* Before deploying, clone the repo on the candidate and run `uv run python scripts/latency_check.py --budget 4 <links>`.
  Use 4 s for a 6 s stopwatch target, which leaves about 2 s for Telegram and the client. It needs no token and sends
  nothing.
* See [troubleshooting: delivery takes minutes](troubleshooting.md#delivery-takes-minutes-instead-of-seconds) for
  how to read it, and for the residential-connection numbers that motivated this check.

Before putting a bot token on the server, harden the host:

* SSH keys only (`PasswordAuthentication no`, `PermitRootLogin no`), a non-root sudo user (or let `ops/setup-ubuntu.sh`
  create the unprivileged `tmd` service user), and automatic security updates:
  ```bash
  sudo apt-get update && sudo apt-get install -y ufw unattended-upgrades git
  ```
* Firewall (no inbound app ports are needed; never expose a local Bot API server, worker, or egress proxy):
  ```bash
  sudo ufw default deny incoming
  sudo ufw allow OpenSSH
  sudo ufw enable
  ```
* Monitor disk usage on the filesystem hosting the repository (`data/` and `tmp/jobs/`): active job files live under
  `tmp/jobs/<32hex>`.

## 2. Get the code at a tested version

```bash
sudo mkdir -p /opt/tmd && sudo chown "$USER:$USER" /opt/tmd
git clone <your repository url> /opt/tmd && cd /opt/tmd
git checkout <tag or commit you tested>     # avoid deploying an untested moving branch
```

Use a shared path such as `/opt/tmd` (not `/root`): when `ops/setup-ubuntu.sh` runs directly as `root`, it switches to
the unprivileged `tmd` system user, which cannot traverse `/root`.

## 3. Bootstrap Ubuntu/Debian with `ops/setup-ubuntu.sh`

```bash
sudo bash ops/setup-ubuntu.sh
```

The script is idempotent (safe to re-run) and performs:

1. **System packages & `uv`:** installs `ffmpeg` (includes `ffprobe`), `git`, `curl`, `ca-certificates`, and `uv` (to
   `/usr/local/bin/uv` if not already installed).
2. **Unprivileged service user:** uses `$SUDO_USER` when invoked via `sudo` from a non-root account, or creates and uses
   a dedicated unprivileged system user `tmd` when invoked directly as `root`.
3. **Python 3.12 environment:** runs `uv sync --locked --no-dev` as the service user, creating `.venv/` with exact
   locked dependencies (`aiogram`, `yt-dlp`, `gallery-dl`, etc.).
4. **Idempotent `.env` setup:** copies `.env.example` to `.env` (`chmod 0600`) only if `.env` does not already exist;
   preserves your existing tuned values on re-runs (backfilling only missing/empty keys), sets `APP_ENV=production` and
   `WORKER_MODE=inprocess` on a fresh `.env`, and prompts (with hidden input) for `BOT_TOKEN` (unless `BOT_TOKEN_FILE`
   is set) and access settings (`ALLOWED_USER_IDS` or `PUBLIC_ACCESS=true`) if still set to placeholders.
5. **Directories & validation:** pre-creates `data/`, `tmp/jobs/`, and `secrets/` (`0700`, files inside `secrets/` set
   to `0600`) with proper ownership and validates `.env` with `Settings().validate_for_startup()`.
6. **Hardened `systemd` unit:** writes `/etc/systemd/system/tmd-bot.service`, runs `systemctl daemon-reload`, enables
   `tmd-bot`, and starts it.

Key hardening settings in `/etc/systemd/system/tmd-bot.service`:

```ini
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths=/opt/tmd/data /opt/tmd/tmp
MemoryMax=2G
TasksMax=256
Restart=on-failure
RestartSec=5
```

Verify immediately after setup:

```bash
sudo systemctl status tmd-bot --no-pager
sudo journalctl -u tmd-bot -n 40 --no-pager
sudo -u tmd bash -c 'cd /opt/tmd && .venv/bin/python -m app.healthcheck'   # exit 0 = healthy (replace `tmd` with your RUN_USER if using SUDO_USER)
```

Then send one real URL from your phone and watch `sudo journalctl -u tmd-bot -f`.

## 4. Configuration, secrets & Instagram Stories session

`.env` must be owned by the service user with mode `0600`. Key production settings:

* `APP_ENV=production` — enforces valid `BOT_TOKEN`, `PRIVATE_CHAT_ONLY=true`, and either `PUBLIC_ACCESS=true` or a
  non-empty `ALLOWED_USER_IDS` (comma-separated numeric Telegram user IDs). Single-process `WORKER_MODE=inprocess` is
  supported in `APP_ENV=production`.
* `ALERTS_ENABLED=true` (optional) — sends failure-streak and low-disk alerts to `ALLOWED_USER_IDS` (never includes URLs).
* **File-based secrets (recommended):** instead of storing raw secrets in `.env`, place them in `secrets/` with
  restrictive permissions and point `.env` at them:

| File | Mode / owner | `.env` variable |
| --- | --- | --- |
| `secrets/bot_token` | `0400` `<service-user>` | `BOT_TOKEN_FILE=secrets/bot_token` |
| `secrets/instagram_cookies.txt` (optional) | `0400` `<service-user>` | `INSTAGRAM_COOKIES_FILE=secrets/instagram_cookies.txt` |
| `secrets/worker_secret` (optional, remote worker mode) | `0440` `<bot-user>:<shared-group>` | `WORKER_SECRET_FILE=secrets/worker_secret` |

```bash
mkdir -p secrets && chmod 0700 secrets
# Create secret files with your editor or hidden prompt, then lock down permissions:
sudo chown -R tmd:tmd secrets
sudo chmod 0400 secrets/*
```

`secrets/` and `.env` are git-ignored (and enforced by `.githooks/pre-commit`). Never paste tokens, cookies, or worker
secrets into a chat, ticket, or issue. To rotate the bot token: revoke it in `@BotFather`, update `.env` or
`secrets/bot_token`, and run `sudo systemctl restart tmd-bot`.

Apply any `.env` change safely:

```bash
sudo -u tmd bash -c 'cd /opt/tmd && .venv/bin/python -c "from app.config import Settings; Settings().validate_for_startup()"'
sudo systemctl restart tmd-bot
```

### Optional: Instagram Stories session

Stories need an ordinary logged-in session; posts, Reels and X never use it. Export **your own** cookies in Netscape
format to `secrets/instagram_cookies.txt`, restrict permissions (`chmod 0400`, owned by the service user), set
`INSTAGRAM_COOKIES_FILE=secrets/instagram_cookies.txt` in `.env`, and run `sudo systemctl restart tmd-bot`.

Each job copies the cookie file into its private per-job workspace (`0600`) so `yt-dlp` and `gallery-dl` never mutate
the original file. Sessions expire periodically: when Stories begin answering *"owner must refresh the session"*, log in
with a normal browser, resolve any prompt yourself, and re-export the file.

## 5. Optional: native multi-process isolation (remote worker + egress proxy)

Default deployment runs in a single hardened `systemd` unit (`WORKER_MODE=inprocess`). If you want the extractors to
run in a separate OS process/user with **no Telegram token and no SQLite access**, the repository includes two pure-Python
loopback services:

```text
Telegram <-> app.main (bot) --127.0.0.1:8090--> app.extractors.service --127.0.0.1:3128--> app.services.egress_proxy --> Platforms
             token + data/                      yt-dlp, gallery-dl, ffmpeg                 allowlist + public-IP pin
```

All three bind to `127.0.0.1` by default — never expose ports `8090` or `3128` on `0.0.0.0`.

1. Generate a shared secret (>= 24 chars):
   ```bash
   .venv/bin/python -c "import secrets; print(secrets.token_urlsafe(32))" > secrets/worker_secret
   ```
2. If running bot and worker as two separate Linux users (e.g. `tmd-bot` and `tmd-worker`), add both to a shared group
   (e.g. `tmd-media`), set `tmp/jobs/` to `2770` (`chgrp tmd-media tmp/jobs && chmod 2770 tmp/jobs`), keep `data/` and
   `secrets/bot_token` readable only by `tmd-bot`, and keep `secrets/worker_secret` `0440` for the shared group.
3. Run the three processes (typically as three separate `systemd` units):
   * **Egress proxy:** `EGRESS_LISTEN_HOST=127.0.0.1 EGRESS_LISTEN_PORT=3128 .venv/bin/python -m app.services.egress_proxy`
   * **Worker:** `WORKER_HOST=127.0.0.1 WORKER_PORT=8090 WORKER_SECRET_FILE=secrets/worker_secret EGRESS_PROXY_URL=http://127.0.0.1:3128 .venv/bin/python -m app.extractors.service`
   * **Bot (`.env`):** `APP_ENV=production`, `WORKER_MODE=remote`, `WORKER_URL=http://127.0.0.1:8090`, `WORKER_SECRET_FILE=secrets/worker_secret`

`APP_ENV=production` with `WORKER_MODE=remote` enforces that `WORKER_SECRET` is at least 24 characters and `WORKER_URL`
is valid.

## 6. Hosted vs local Telegram Bot API

* **Hosted** (default, `BOT_API_MODE=hosted`): uploads up to 50 MB per file (photos 10 MB, `w+h <= 10,000`, ratio `<= 20`).
* **Local** (`BOT_API_MODE=local`, `LOCAL_BOT_API_URL=http://127.0.0.1:8081`): uploads up to 2,000 MB (`LOCAL_FILE_MAX_BYTES`
  defaults to 1,900 MB). Build and run [tdlib/telegram-bot-api](https://github.com/tdlib/telegram-bot-api) natively as a
  separate `systemd` service bound strictly to `--http-ip-address=127.0.0.1 --http-port=8081` (never exposed publicly),
  passing your API ID and hash from <https://my.telegram.org> via its own restricted environment/service config (never
  read by the bot).

**Switching hosted -> local** (never poll from both at once):

```bash
sudo systemctl stop tmd-bot
# 1. Log the bot out of the cloud Bot API (required before moving to a local server):
sudo -u tmd bash -c 'cd /opt/tmd && .venv/bin/python -c "
import urllib.request as u
from app.config import Settings
tok = Settings().resolved_token()
print(u.urlopen(f\"https://api.telegram.org/bot{tok}/logOut\", timeout=20).read().decode())
"'
# 2. Start your local telegram-bot-api service on 127.0.0.1:8081.
# 3. Set BOT_API_MODE=local and LOCAL_BOT_API_URL=http://127.0.0.1:8081 in .env, then:
sudo systemctl start tmd-bot
```

Per Telegram's Bot API documentation, after `logOut` you can log in on the local server immediately but **cannot return
to the cloud server for 10 minutes**. To move **local -> hosted**, stop `tmd-bot` and the local API server, set
`BOT_API_MODE=hosted` in `.env`, wait out any cooldown, start `tmd-bot`, and confirm `getMe` succeeds in `journalctl`.
Run `uv run python scripts/telegram_smoke.py --large` after enabling local mode and monitor the local Bot API server's
working directory for disk growth (automatic cache cleanup is unverified; T01.9/T05.6).

## 7. Updating, and rolling back

Record your current commit before updating so rollback is instant:

```bash
cd /opt/tmd
git rev-parse --short HEAD                 # note this commit for rollback
git fetch --tags
git checkout <new-tag-or-commit>           # or: git pull --ff-only
sudo -u tmd bash -c 'cd /opt/tmd && uv sync --locked --no-dev'
sudo -u tmd bash -c 'cd /opt/tmd && .venv/bin/python -c "from app.config import Settings; Settings().validate_for_startup()"'
sudo systemctl restart tmd-bot
sudo systemctl status tmd-bot --no-pager
sudo journalctl -u tmd-bot -n 30 --no-pager
sudo -u tmd bash -c 'cd /opt/tmd && .venv/bin/python -m app.healthcheck'
```

**Rollback to the previous tested version:**

```bash
cd /opt/tmd
git checkout <previous-commit-or-tag>
sudo -u tmd bash -c 'cd /opt/tmd && uv sync --locked --no-dev'
sudo systemctl restart tmd-bot
sudo systemctl status tmd-bot --no-pager
```

Database migrations are additive and move forward only. If you want a short-lived pre-upgrade snapshot, stop `tmd-bot`
first and copy `data/bot.sqlite3` (plus `-wal`/`-shm` if present), deleting the backup once verified (respecting the
24-hour retention policy below). Jobs in flight during a restart are marked *interrupted* on startup recovery and the
owner receives a Retry button; nothing is replayed automatically.

### Updating the extractors safely

TikTok, Instagram and X change often; `EXTRACTOR_CHANGED` errors are the signal. Never float on `latest`.

1. Bump the exact pin in `pyproject.toml` (`yt-dlp==...`, `gallery-dl==...`), then regenerate both lock files:
   ```bash
   uv lock
   uv export --format requirements-txt --no-dev --no-emit-project --hashes -o requirements.lock
   ```
2. Sync and run the full offline suite (`test_cli_flags.py` catches removed CLI flags; `test_dependency_pins.py` verifies
   `requirements.lock`):
   ```bash
   uv sync && uv run pytest
   ```
3. Run the feasibility probe and opt-in live suite against your corpus:
   ```bash
   uv run python scripts/feasibility_probe.py offline
   uv run python scripts/feasibility_probe.py live --download
   uv run pytest -m live
   ```
   Compare with the previous matrix in `ops/feasibility.md`.
4. Commit the tested pins, deploy the commit to the VPS (`git pull --ff-only` / `git checkout`, `uv sync --locked --no-dev`,
   `sudo systemctl restart tmd-bot`), and send one URL per platform.
5. If anything regresses: roll back to the previous commit as shown above and record the failure in `ops/dev-log.md`.

## 8. Backups, retention, logs, and privacy

* **What is stored:** `data/bot.sqlite3` (SQLite WAL) holds job metadata (submitted URLs, chat/user IDs, states, delivery
  records) for at most `JOB_METADATA_TTL_HOURS` (24 h by default; purged periodically by housekeeping). `tmp/jobs/` holds
  media only while a job runs (removed on completion, cancel, failure, shutdown, and startup/periodic sweeps).
* **Back up neither `tmp/jobs/` nor `secrets/`/`.env` into repo archives or git.** Keep secrets in a password manager or
  separate encrypted store.
* **The database is usually not worth backing up** (24 h of URLs). Exclude `data/` from long-lived host backups, or age
  backups out on the same 24 h schedule.
* **Deleting local files does not delete what Telegram already holds.** Delivered media remain in the chat and on
  Telegram's servers until you delete the messages.
* **Logs:** `app/logging_setup.py` redacts tokens, cookies, secret paths, and full source URLs (including fatal startup
  errors). On Linux VPS, logs go to `systemd-journald`; cap disk usage in `/etc/systemd/journald.conf` (e.g.
  `SystemMaxUse=200M`, then `sudo systemctl restart systemd-journald`).

## 9. Day-two operations (Linux VPS)

| Task | Command |
| --- | --- |
| Service status | `sudo systemctl status tmd-bot` |
| Health check (no network) | `sudo -u tmd bash -c 'cd /opt/tmd && .venv/bin/python -m app.healthcheck'` |
| Follow live logs | `sudo journalctl -u tmd-bot -f` |
| Recent job outcomes | `sudo journalctl -u tmd-bot --since "1 hour ago" \| grep '"job finished"'` |
| Denied proxy hosts (if proxy enabled) | `sudo journalctl -u tmd-proxy \| grep "egress denied"` |
| Restart bot | `sudo systemctl restart tmd-bot` |
| Stop bot | `sudo systemctl stop tmd-bot` |
| Resource limits | Edit `/etc/systemd/system/tmd-bot.service` (`MemoryMax`, `TasksMax`), then `sudo systemctl daemon-reload && sudo systemctl restart tmd-bot` |
| Disk headroom | `df -h /opt/tmd/tmp` and `du -sh /opt/tmd/data /opt/tmd/tmp` |
| Optional alerts | Set `ALERTS_ENABLED=true` in `.env` and restart `tmd-bot` |

## 10. Native Windows operation

For running locally on Windows 10/11 (testing or personal desktop use):

1. **Install prerequisites:**
   * [uv](https://docs.astral.sh/uv/)
   * FFmpeg/FFprobe on `PATH`: `winget install Gyan.FFmpeg` (open a new terminal afterwards and confirm with `where.exe ffmpeg` and `where.exe ffprobe`)
   * Git
2. **Configure `.env`:** Copy `.env.example` to `.env` (`copy .env.example .env`), then set `BOT_TOKEN` and
   `ALLOWED_USER_IDS` (or `PUBLIC_ACCESS=true`). For local testing you can keep `APP_ENV=development` or use
   `APP_ENV=production` with `WORKER_MODE=inprocess`. (`start-bot.bat` exits with an error if `.env` is missing.)
3. **Run the bot:**
   * Double-click or run `start-bot.bat` from Command Prompt / PowerShell.
   * On first launch (or when called as `start-bot.bat --sync`), it creates/updates `.venv` with `uv sync --locked --no-dev`,
     checks `ffmpeg`/`ffprobe` and `.env`, ensures `data\` and `tmp\jobs\` exist, validates settings, and runs
     `.venv\Scripts\python.exe -m app.main`.
   * Stop cleanly with `Ctrl+C` (marks any running job interrupted and cleans up `tmp\jobs\`).
4. **Health check & manual commands (PowerShell):**
   ```powershell
   .venv\Scripts\python.exe -m app.healthcheck
   .venv\Scripts\python.exe -c "from app.config import Settings; Settings().validate_for_startup()"
   ```
5. **Updates on Windows:**
   ```powershell
   git pull --ff-only
   uv sync --locked --no-dev    # or `uv sync` if you also run pytest/ruff
   .\start-bot.bat
   ```
6. **Unattended background startup (optional):** Use Windows Task Scheduler to run `.venv\Scripts\python.exe` with
   arguments `-m app.main` and "Start in" set to the repository path. **Never** run the Windows instance and a VPS
   instance simultaneously with the same `BOT_TOKEN` — Telegram rejects duplicate `getUpdates` pollers with a conflict error.

## 11. Acceptance of the VPS deployment (T06.9-T06.13)

Tick these off on the real VPS and record the output in `ops/dev-log.md`:

1. `sudo bash ops/setup-ubuntu.sh` succeeds cleanly; `sudo systemctl status tmd-bot` shows `active (running)` under an
   unprivileged user (`tmd` or your non-root deploy user) with `systemd` hardening active.
2. `.venv/bin/python -m app.healthcheck` exits `0`, and exactly one polling instance is running.
3. A real URL per platform delivers (T06.12), including a carousel, a Story, and *Original files*.
4. `sudo systemctl restart tmd-bot` during a job: the job is reported *interrupted*, temp files in `tmp/jobs/` are gone,
   and nothing is sent twice (T06.13).
5. Rollback to the previous git commit (`git checkout <prev> && uv sync --locked --no-dev && sudo systemctl restart tmd-bot`) works.
6. Android and iOS clients display the media and clickable links (T06.6).
