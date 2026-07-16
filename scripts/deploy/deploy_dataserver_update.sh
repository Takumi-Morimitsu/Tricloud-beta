#!/usr/bin/env bash
set -Eeuo pipefail

NEW_FILE="${1:-$HOME/server.py}"
APP_DIR="/opt/tricloud/backend"
TARGET="$APP_DIR/server.py"
SERVICE="tricloud-dataserver.service"
APP_USER="${TRICLOUD_APP_USER:-tricloud}"
APP_GROUP="${TRICLOUD_APP_GROUP:-$APP_USER}"
PYTHON="$APP_DIR/.venv/bin/python"
STAMP="$(date +%Y%m%d-%H%M%S)"
BACKUP="$APP_DIR/server.py.bak-$STAMP"
STAGED="$APP_DIR/server.py.new-$STAMP"
PYCACHE="/tmp/tricloud-pycache-$STAMP"

cleanup() {
  sudo rm -f "$STAGED" 2>/dev/null || true
  rm -rf "$PYCACHE" 2>/dev/null || true
}
trap cleanup EXIT

if [[ ! -f "$NEW_FILE" ]]; then
  echo "ERROR: update file not found: $NEW_FILE" >&2
  exit 1
fi

if [[ ! -x "$PYTHON" ]]; then
  echo "ERROR: virtualenv Python not found: $PYTHON" >&2
  exit 1
fi

if [[ ! -f "$TARGET" ]]; then
  echo "ERROR: current DataServer file not found: $TARGET" >&2
  exit 1
fi

echo "[1/6] Staging new server.py"
sudo install -o "$APP_USER" -g "$APP_GROUP" -m 0644 "$NEW_FILE" "$STAGED"

echo "[2/6] Syntax-checking with the production virtualenv"
sudo -u "$APP_USER" env PYTHONPYCACHEPREFIX="$PYCACHE" \
  "$PYTHON" -m py_compile "$STAGED"

echo "[3/6] Backing up current server.py"
sudo cp -a "$TARGET" "$BACKUP"

echo "[4/6] Installing the update"
sudo mv "$STAGED" "$TARGET"
sudo chown "$APP_USER:$APP_GROUP" "$TARGET"
sudo chmod 0644 "$TARGET"

echo "[5/6] Restarting $SERVICE"
if ! sudo systemctl restart "$SERVICE"; then
  echo "ERROR: restart command failed. Rolling back." >&2
  sudo cp -a "$BACKUP" "$TARGET"
  sudo systemctl restart "$SERVICE" || true
  exit 1
fi

sleep 2
if ! sudo systemctl is-active --quiet "$SERVICE"; then
  echo "ERROR: service is not active after update. Rolling back." >&2
  sudo journalctl -u "$SERVICE" -n 80 --no-pager || true
  sudo cp -a "$BACKUP" "$TARGET"
  sudo systemctl restart "$SERVICE" || true
  exit 1
fi

echo "[6/6] Update succeeded"
sudo systemctl --no-pager --full status "$SERVICE" || true
echo
echo "Backup: $BACKUP"
echo
echo "Recent DataServer logs:"
sudo journalctl -u "$SERVICE" -n 80 --no-pager
