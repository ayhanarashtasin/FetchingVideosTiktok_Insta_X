#!/usr/bin/env bash
# Native Ubuntu/Debian VPS bootstrap and hardened systemd service setup.
# Usage: sudo bash ops/setup-ubuntu.sh
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Run as root or with sudo: sudo bash ops/setup-ubuntu.sh" >&2
  exit 1
fi

REPO_DIR="$(cd "$(dirname "$0")/.." && pwd)"

if [ -n "${SUDO_USER:-}" ] && [ "$SUDO_USER" != "root" ]; then
  RUN_USER="$SUDO_USER"
  RUN_GROUP="$(id -gn "$RUN_USER")"
  RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
else
  RUN_USER="tmd"
  if ! id -u "$RUN_USER" >/dev/null 2>&1; then
    useradd --system --user-group --create-home --home-dir /var/lib/tmd --shell /usr/sbin/nologin "$RUN_USER"
  fi
  RUN_GROUP="$(id -gn "$RUN_USER")"
  RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
  mkdir -p "$RUN_HOME"
  chown "$RUN_USER:$RUN_GROUP" "$RUN_HOME"
  chmod 0750 "$RUN_HOME"

  REPO_PARENT="$(dirname "$REPO_DIR")"
  if ! su -s /bin/bash "$RUN_USER" -c "cd $(printf '%q' "$REPO_PARENT")" 2>/dev/null; then
    echo "ERROR: $RUN_USER cannot access $REPO_DIR (for example, it is inside /root)." >&2
    echo "Move or clone the repository to a shared path such as /opt/tmd and re-run." >&2
    exit 1
  fi
  chown -R "$RUN_USER:$RUN_GROUP" "$REPO_DIR"
fi

run_as_user() {
  local cmd
  printf -v cmd '%q ' "$@"
  su -s /bin/bash "$RUN_USER" -c "export HOME=$(printf '%q' "$RUN_HOME"); cd $(printf '%q' "$REPO_DIR") && $cmd"
}

if ! run_as_user test -w "$REPO_DIR" 2>/dev/null; then
  chown -R "$RUN_USER:$RUN_GROUP" "$REPO_DIR"
fi

echo "== Installing system packages (ffmpeg, git, curl, ca-certificates) =="
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffmpeg git curl ca-certificates

UV_BIN="$(command -v uv || true)"
if [ -n "$UV_BIN" ] && ! run_as_user test -x "$UV_BIN" 2>/dev/null; then
  UV_BIN=""
fi
if [ -z "$UV_BIN" ] && [ -x "$RUN_HOME/.local/bin/uv" ]; then
  UV_BIN="$RUN_HOME/.local/bin/uv"
fi
if [ -z "$UV_BIN" ]; then
  echo "== Installing uv to /usr/local/bin =="
  curl -LsSf https://astral.sh/uv/install.sh | env UV_INSTALL_DIR=/usr/local/bin UV_NO_MODIFY_PATH=1 sh
  UV_BIN=/usr/local/bin/uv
  chmod 0755 "$UV_BIN"
fi

echo "== Syncing Python 3.12 environment as $RUN_USER =="
run_as_user "$UV_BIN" sync --locked --no-dev

echo "== Preparing runtime directories =="
mkdir -p "$REPO_DIR/data" "$REPO_DIR/tmp/jobs" "$REPO_DIR/secrets"
chown -R "$RUN_USER:$RUN_GROUP" "$REPO_DIR/data" "$REPO_DIR/tmp" "$REPO_DIR/secrets"
chmod 0700 "$REPO_DIR/data" "$REPO_DIR/tmp" "$REPO_DIR/tmp/jobs" "$REPO_DIR/secrets"
find "$REPO_DIR/secrets" -type f -exec chmod 0600 {} +

ENV_FILE="$REPO_DIR/.env"
FRESH_ENV=0
if [ ! -f "$ENV_FILE" ]; then
  cp "$REPO_DIR/.env.example" "$ENV_FILE"
  FRESH_ENV=1
fi
chown "$RUN_USER:$RUN_GROUP" "$ENV_FILE"
chmod 0600 "$ENV_FILE"

PY_BIN="$REPO_DIR/.venv/bin/python"
set_env_kv() {
  local key="$1" val="$2" mode="${3:-overwrite}"
  ENV_PATH="$ENV_FILE" ENV_KEY="$key" ENV_VAL="$val" ENV_MODE="$mode" "$PY_BIN" - <<'PY'
import os
from pathlib import Path

path = Path(os.environ["ENV_PATH"])
key = os.environ["ENV_KEY"]
val = os.environ["ENV_VAL"]
overwrite = os.environ["ENV_MODE"] == "overwrite"
lines = path.read_text(encoding="utf-8").splitlines()
out = []
found = False
for line in lines:
    if line.startswith(f"{key}="):
        existing = line.split("=", 1)[1].strip()
        if existing and not overwrite:
            out.append(line)
        else:
            out.append(f"{key}={val}")
        found = True
    else:
        out.append(line)
if not found:
    out.append(f"{key}={val}")
path.write_text("\n".join(out) + "\n", encoding="utf-8")
PY
}

get_env_val() {
  ENV_PATH="$ENV_FILE" ENV_KEY="$1" "$PY_BIN" - <<'PY'
import os
from pathlib import Path

key = os.environ["ENV_KEY"] + "="
for line in Path(os.environ["ENV_PATH"]).read_text(encoding="utf-8").splitlines():
    if line.startswith(key):
        print(line.split("=", 1)[1].strip())
        break
PY
}

# Set production + VPS-safe defaults on a fresh .env, and only backfill missing/empty keys on re-run.
ENV_MODE=default
if [ "$FRESH_ENV" -eq 1 ]; then
  ENV_MODE=overwrite
fi
set_env_kv APP_ENV production "$ENV_MODE"
set_env_kv WORKER_MODE inprocess "$ENV_MODE"
set_env_kv MAX_CONCURRENT_JOBS 2 "$ENV_MODE"
set_env_kv MAX_TEMP_BYTES_PER_JOB 1000000000 "$ENV_MODE"
set_env_kv MIN_FREE_DISK_BYTES 2000000000 "$ENV_MODE"
set_env_kv NATIVE_HEVC true "$ENV_MODE"

CUR_TOKEN_FILE="$(get_env_val BOT_TOKEN_FILE)"
CUR_TOKEN="$(get_env_val BOT_TOKEN)"
if [ -z "$CUR_TOKEN_FILE" ] && { [ -z "$CUR_TOKEN" ] || [ "$CUR_TOKEN" = "replace_me" ]; }; then
  read -r -s -p "Paste BOT_TOKEN from BotFather (input hidden): " NEW_TOKEN
  echo
  [ -n "$NEW_TOKEN" ] || { echo "BOT_TOKEN cannot be empty" >&2; exit 1; }
  set_env_kv BOT_TOKEN "$NEW_TOKEN" overwrite
  unset NEW_TOKEN
fi
unset CUR_TOKEN CUR_TOKEN_FILE

CUR_PUBLIC="$(get_env_val PUBLIC_ACCESS)"
CUR_USERS="$(get_env_val ALLOWED_USER_IDS)"
if [ "${CUR_PUBLIC,,}" != "true" ] && { [ -z "$CUR_USERS" ] || [ "$CUR_USERS" = "123456789" ]; }; then
  read -r -p "Enter ALLOWED_USER_IDS (comma-separated numeric IDs, or 'public' for any private chat): " ACCESS_IN
  if [ "${ACCESS_IN,,}" = "public" ]; then
    set_env_kv PUBLIC_ACCESS true overwrite
  elif [ -n "$ACCESS_IN" ]; then
    set_env_kv PUBLIC_ACCESS false overwrite
    set_env_kv ALLOWED_USER_IDS "$ACCESS_IN" overwrite
  fi
fi

chown "$RUN_USER:$RUN_GROUP" "$ENV_FILE"
chmod 0600 "$ENV_FILE"

echo "== Validating configuration =="
run_as_user "$PY_BIN" -c "from app.config import Settings; Settings().validate_for_startup()"

UNIT_FILE="/etc/systemd/system/tmd-bot.service"
cat > "$UNIT_FILE" <<EOF
[Unit]
Description=Telegram Media Downloader Bot
After=network-online.target telegram-bot-api.service
Wants=network-online.target

[Service]
Type=simple
User=${RUN_USER}
Group=${RUN_GROUP}
WorkingDirectory=${REPO_DIR}
Environment=PATH=${RUN_HOME}/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=PYTHONUNBUFFERED=1
Environment=PYTHONDONTWRITEBYTECODE=1
ExecStart="${PY_BIN}" -m app.main
Restart=on-failure
RestartSec=5
TimeoutStopSec=35
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ReadWritePaths="${REPO_DIR}/data" "${REPO_DIR}/tmp"
MemoryMax=2G
TasksMax=256

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable tmd-bot
systemctl restart tmd-bot

echo
echo "Done! Service status:"
systemctl --no-pager --full status tmd-bot || true
echo
echo "Useful commands:"
echo "  sudo journalctl -u tmd-bot -f                                  # live logs"
echo "  sudo systemctl restart tmd-bot                                 # restart bot"
echo "  sudo systemctl stop tmd-bot                                    # stop bot"
echo "  cd ${REPO_DIR} && sudo -u ${RUN_USER} ${PY_BIN} -m app.healthcheck"
