# Troubleshooting

Golden rules: **never bypass** a login wall, CAPTCHA, challenge or rate limit; **never paste** a token, cookie, worker
secret or full source URL into a chat, ticket or issue. Logs are redacted (tokens, cookies, secret paths, URL query
strings) and fatal errors go through the same redactor, but still skim any log before sharing it.

Start with:

* **Linux VPS (`systemd`):**
  ```bash
  sudo systemctl status tmd-bot --no-pager
  sudo journalctl -u tmd-bot -n 50 --no-pager
  sudo -u tmd bash -c 'cd /opt/tmd && .venv/bin/python -m app.healthcheck'
  ```
* **Windows:** check the `start-bot.bat` terminal output, and in a second terminal run:
  ```powershell
  .venv\Scripts\python.exe -m app.healthcheck
  where.exe ffmpeg ffprobe
  ```

## The bot does not start or does not answer at all

| Symptom / Check | What it means / action |
| --- | --- |
| Exits immediately; log says `refusing to start: invalid configuration` | `APP_ENV=production` rejects a placeholder `BOT_TOKEN`, an empty `ALLOWED_USER_IDS` when `PUBLIC_ACCESS=false`, `PRIVATE_CHAT_ONLY=false`, or (when `WORKER_MODE=remote`) a weak `WORKER_SECRET` (< 24 chars) or invalid `WORKER_URL`. Note: `WORKER_MODE=inprocess` **is** allowed in `APP_ENV=production`. The log names the specific field (never the secret). Validate edits with `.venv/bin/python -c "from app.config import Settings; Settings().validate_for_startup()"` (or `.venv\Scripts\python.exe` on Windows). |
| Log says `Telegram rejected the bot token` (exit code 3) | The token is wrong or was revoked in `@BotFather`. Update `BOT_TOKEN` in `.env` (or `secrets/bot_token`) and restart (`sudo systemctl restart tmd-bot` / re-run `start-bot.bat`). |
| Log shows `Conflict: terminated by other getUpdates request` | A second instance is polling with the same `BOT_TOKEN` (for example, `start-bot.bat` running on Windows while `tmd-bot` is also active on the VPS, two terminals open locally, or a local Bot API switch without `logOut`). Stop the extra process (`ps aux \| grep app.main` on Linux; Task Manager or `Get-Process python*` on Windows). Only one poller may exist per token. |
| Service is healthy, but you get no reply and nothing appears for your message | Your numeric ID is not in `ALLOWED_USER_IDS` (when `PUBLIC_ACCESS=false`), or you messaged in a group chat (only private chats are served). Unapproved senders are ignored **silently by design**. Confirm your numeric ID, update `.env`, and restart the bot. |
| `python -m app.healthcheck` prints `UNHEALTHY: ...` (exit 1) | Reads `data/heartbeat` (no network). Common reasons: `no heartbeat file` (bot not running or `DATA_DIR` mismatch), `heartbeat is stale` (event loop hung or process dead), `polling stalled` (network outage or Telegram API unreachable), `database is not writable` (SQLite permissions/disk), `extractor worker is unreachable` (in `WORKER_MODE=remote`), or `low disk space`. |
| After switching to the local Bot API: nothing arrives | The bot must be logged out of Telegram's cloud server first and cannot return to cloud for 10 minutes; see `ops/deploy.md` section 6. Also verify `LOCAL_BOT_API_URL=http://127.0.0.1:8081` is reachable locally. |

## `ffmpeg`, `ffprobe`, `uv`, and `.venv` issues

| Symptom | Cause | Action |
| --- | --- | --- |
| Startup warning or `start-bot.bat` error: `ffmpeg` or `ffprobe` not found on `PATH` | Media tools are missing or not yet visible in the shell's `PATH`. | **Windows:** install with `winget install Gyan.FFmpeg`, then open a **new** terminal so `PATH` refreshes; verify with `where.exe ffmpeg` and `where.exe ffprobe` (in PowerShell, bare `where` aliases `Where-Object`). **Ubuntu/Debian:** `sudo apt-get update && sudo apt-get install -y ffmpeg`; confirm `which ffmpeg ffprobe` is in `/usr/bin` (included in `tmd-bot.service`'s `Environment=PATH=`). |
| `start-bot.bat` says `uv not found on PATH` | `uv` is not installed or not on `PATH`. | Install from <https://docs.astral.sh/uv/> and reopen the terminal. |
| `ModuleNotFoundError` or stale extractor after `git pull` | `.venv` was not synced with the updated `uv.lock`. | **Windows:** run `start-bot.bat --sync` (or `uv sync --locked --no-dev`, or `uv sync` for dev). **Linux VPS:** `sudo -u tmd bash -c 'cd /opt/tmd && uv sync --locked --no-dev'` then `sudo systemctl restart tmd-bot`. |
| `PermissionError` on Linux VPS (`data/`, `tmp/jobs/`, `.env`, `secrets/`) or `tmd cannot access <dir>` | Wrong file ownership, repository cloned inside `/root`, or `systemd` `ReadWritePaths=` mismatch. | Clone to a shared path like `/opt/tmd` (not `/root`). Check `User=` in `/etc/systemd/system/tmd-bot.service`, ensure it owns `.env` (`0600`), `data/`, `tmp/jobs/`, and `secrets/*`, and verify `ReadWritePaths=` points to `<repo>/data` and `<repo>/tmp`. Re-running `sudo bash ops/setup-ubuntu.sh` repairs ownership and the unit file without overwriting your `.env` values. |

## SQLite WAL & database issues

* `data/bot.sqlite3-wal` and `data/bot.sqlite3-shm` alongside `data/bot.sqlite3` are normal in WAL mode. Do not delete
  them while the bot is running.
* **`database is locked`:** Usually caused by running two bot processes against the same `data/bot.sqlite3`. Stop the
  duplicate process so only one instance accesses the database.
* **`database is not writable` (in healthcheck or logs):** Check disk space (`df -h`), ownership/permissions on `data/`
  and `data/bot.sqlite3`, and (on Linux VPS) `ProtectSystem=strict` + `ReadWritePaths=` in `/etc/systemd/system/tmd-bot.service`.
* If you need to inspect or copy `data/bot.sqlite3`, stop the bot first (`sudo systemctl stop tmd-bot` or `Ctrl+C` on
  Windows) so SQLite checkpoints cleanly.

## A link was rejected or failed

The user-facing text and what to do:

| Message | Cause | Action |
| --- | --- | --- |
| "Please send a TikTok, Instagram, or X post link." | Host not on the exact allowlist (look-alike or lookalike-suffix hosts, IP addresses, other sites). | Expected. Mobile/regional variants must be added deliberately via `EXTRA_ALLOWED_HOSTS=host=platform` after verifying them. |
| "Please send one link at a time." | More than one URL in the message. | Expected. |
| "This link is not a supported individual post or Story." | Profile, highlight, live, search or an unknown path. | Expected. Stories must point at one specific Story. |
| "This post or Story is unavailable or may have expired." | Deleted, expired (Stories live ~24 h), geo-blocked or the extractor could not find it. | Open it in a normal browser to confirm. Nothing to fix on the bot side. |
| "This content is private or restricted. I can only retrieve permitted public media." | Private account, close-friends/subscriber content, age/sensitive gate, or a **login wall** shown to anonymous visitors. | Not retried, no second extractor tried, no session used for posts (by design). |
| "This public Story requires an authorized Instagram session..." | Stories need the owner's session and none is configured, or it expired. | Section "Instagram Stories" below. |
| "The platform is limiting requests. Please try again later." | HTTP 429 / challenge. | Wait; a long `Retry-After` is reported, not slept through. Do not hammer; do not rotate IPs to dodge it. |
| "The platform could not be processed right now..." (`EXTRACTOR_CHANGED`) | The page/API changed and the pinned extractor no longer understands it, or it returned something unexpected. | Section "Extractors broke" below. |
| "...larger than the configured limits (items or size). Nothing was sent." | More than `MAX_ITEMS_PER_JOB` items or `MAX_TEMP_BYTES_PER_JOB` bytes. | Raise the limit only if the disk allows (`MIN_FREE_DISK_BYTES` guards it). |
| "The source file exceeds the configured Telegram upload limit. No quality was reduced." | Hosted API: > 50 MB per file (photos 10 MB, w+h <= 10,000, ratio <= 20). | Use a local Bot API server (`BOT_API_MODE=local`, up to ~2 GB) or accept. Files are never shrunk. |
| "Sending was interrupted and I can't tell whether it arrived. I did not resend it." | A Telegram error where the upload may have succeeded. | Look in the chat first. Press Retry only if it is missing. Replays are never automatic (they could duplicate). |
| "The bot restarted while this was running. Tap Retry" | Restart/redeploy during a job. | Press Retry. |
| *Partial* report ("Sent 4 of 6 ...") | The source declared more items than were delivered/proved, or a send failed mid-album. The message lists which positions are missing. | Retry, or send the link again; the numbering is reported so you can check against the app. |

## Instagram Stories

Stories are fetched only if **all** of these hold, otherwise the bot fails closed with a clear message:
the link names one specific Story; an owner-authorised session is configured; the account is provably **not private**;
the Story id exists in the current reel; there is no close-friends/subscriber marker.

* "requires an authorized session" -> follow `ops/deploy.md` section 4 ("Optional: Instagram Stories session"). Export
  cookies in Netscape format to `secrets/instagram_cookies.txt`, set `INSTAGRAM_COOKIES_FILE=secrets/instagram_cookies.txt`
  in `.env`, ensure permissions allow the service user to read it (`chmod 0400` on Linux), and restart the bot.
* It worked before and now fails -> the session expired or Instagram asked for a checkpoint. Log in with a normal browser,
  resolve any prompt yourself, export the cookies again. The bot never solves challenges.
* "unavailable or may have expired" right after posting -> the Story id may belong to a different reel/user; send the
  exact link from the app.
* Check without leaking: `sudo journalctl -u tmd-bot | grep '"job finished"'` (or check console output on Windows)
  lists, per job, the platform, final state and `error_code` (for example `AUTH_REQUIRED`) — never a URL or cookie value.

## Extractors broke (`EXTRACTOR_CHANGED`, or many unexplained failures after a platform change)

1. Confirm it is not a one-off: send a second known-good public URL of the same platform.
2. Inspect recent outcomes (`sudo journalctl -u tmd-bot -n 200 | grep '"job finished"'` on Linux, or console logs on
   Windows) for the platform, state, and `error_code`. Logs never contain URLs or secrets.
3. Check upstream `yt-dlp` / `gallery-dl` release notes for fixes for that platform.
4. Update **deliberately** using `ops/deploy.md` ("Updating the extractors safely"): bump the exact pin in
   `pyproject.toml`, run `uv lock` + export `requirements.lock`, run `uv run pytest` and the live probe, then deploy the
   tested commit (`uv sync --locked --no-dev` + restart) while noting the previous commit for rollback.
5. Still broken? Record the platform, content type and error class (no URLs, no cookies) in `ops/dev-log.md`; do not
   work around it by weakening the allowlist, enabling yt-dlp's generic extractor, or rotating IPs.

## Disk full / workspaces piling up

* Symptom: log lines `could not remove workspace ...`, `low disk space` healthcheck error or alert, shrinking free space
  (`df -h` on Linux; `Get-PSDrive` in PowerShell).
* The bot sweeps orphaned `tmp/jobs/<32hex>` directories on startup and periodically (after `ORPHAN_TTL_MINUTES`), and
  kills runaway downloads when free disk falls below `MIN_FREE_DISK_BYTES`.
* **Single-process mode:** check permissions on `tmp/jobs/` and verify no crashed external process holds open file
  handles (on Windows, antivirus scanners can briefly lock temp files during cleanup; re-check after a minute or stop
  the bot and clear empty stale folders under `tmp\jobs\`).
* **Optional remote worker mode (Linux):** if bot and worker run under different Linux users, `tmp/jobs/` must be
  group-owned by their shared group with mode `2770` (`stat -c '%U %G %a' tmp/jobs`), so the bot can delete files created
  by the worker.
* **Local Bot API mode:** the `telegram-bot-api` server's own working/cache directory is not cleaned by this bot
  (T05.6 open); inspect its disk usage after large uploads.

## Optional native remote worker & egress proxy (`WORKER_MODE=remote`)

Skip this section if you use the default single-process `WORKER_MODE=inprocess`.

| Symptom | Check / Action |
| --- | --- |
| Jobs fail with `worker_unreachable` | Verify the worker process (`python -m app.extractors.service`) is running and listening on `127.0.0.1:8090` (`ss -tlnp \| grep 8090` on Linux, `netstat -ano \| findstr 8090` on Windows) and matches `WORKER_URL` in `.env`. |
| `401` between bot and worker | Bot and worker read different `WORKER_SECRET` / `WORKER_SECRET_FILE` values. Align both and restart both processes. |
| `"egress denied"` in proxy logs | `egress_proxy` logs `host=` and `reason=` (not on allowlist, IP literal, private address, non-443 port). If a platform added a legitimate CDN host, confirm ownership, set `EGRESS_ALLOWED_SUFFIXES` to `DEFAULT_ALLOWED_SUFFIXES` (from `app/services/egress_proxy.py`) plus the new registrable suffix, and restart the proxy. Never use `*`, a TLD, or wide shared CDNs like `cloudfront.net`/`akamaized.net`. Private/loopback/metadata IPs are always refused. |
| Worker cannot reach any platform | Check that `app.services.egress_proxy` is running on `127.0.0.1:3128` and matches `EGRESS_PROXY_URL`. |

## Collecting a safe report

* **Linux VPS:**
  ```bash
  sudo journalctl -u tmd-bot --since "30 min ago" --no-pager > report.txt
  sudo systemctl status tmd-bot --no-pager >> report.txt
  sudo -u tmd bash -c 'cd /opt/tmd && .venv/bin/python -m app.healthcheck' >> report.txt 2>&1
  sudo -u tmd uv pip list --python /opt/tmd/.venv/bin/python | grep -i -E "yt-dlp|gallery-dl|aiogram" >> report.txt
  ```
* **Windows (PowerShell):**
  ```powershell
  .venv\Scripts\python.exe -m app.healthcheck
  uv pip list --python .venv\Scripts\python.exe | Select-String -Pattern "yt-dlp|gallery-dl|aiogram"
  ffmpeg -version | Select-Object -First 2
  ```

Before sharing any log snippet, search it for your bot token, `sessionid`, `Cookie`, `Authorization`, and full URLs;
delete anything sensitive you find.
