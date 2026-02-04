#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="${CONFIG_PATH:-/config/news_sources.json}"
OUT_DIR="${OUT_DIR:-/data/news_out}"
REFRESH_SECONDS="${REFRESH_SECONDS:-86400}"
PORT="${PORT:-8080}"

mkdir -p "${OUT_DIR}"

build_once() {
  python3 /app/scripts/news_sync_server.py --config "${CONFIG_PATH}"
}

build_once

python3 -m http.server "${PORT}" --directory "${OUT_DIR}" &
SERVER_PID=$!

trap 'kill ${SERVER_PID}; exit 0' INT TERM

while true; do
  sleep "${REFRESH_SECONDS}"
  build_once || true
done
