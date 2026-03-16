#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="${HEARTBEAT_PM2_ROOT:-$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)}"
PM2_BIN="${PM2_BIN:-pm2}"
APP_NAME="${WATCHDOG_PM2_NAME:-heartbeat-watchdog}"
LOG_DIR="${WATCHDOG_PM2_LOG_DIR:-$ROOT_DIR/heartbeat/logs}"

mkdir -p "$LOG_DIR"

if "$PM2_BIN" describe "$APP_NAME" >/dev/null 2>&1; then
  exec "$PM2_BIN" restart "$APP_NAME" --update-env
fi

exec "$PM2_BIN" start "$ROOT_DIR/.venv/bin/python" \
  --name "$APP_NAME" \
  --cwd "$ROOT_DIR" \
  --interpreter none \
  --time \
  --output "$LOG_DIR/watchdog.out.log" \
  --error "$LOG_DIR/watchdog.err.log" \
  -- heartbeat/watchdog.py
