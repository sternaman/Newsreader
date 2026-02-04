#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="${CONFIG_PATH:-/config/news_sources.json}"
OUT_DIR="${OUT_DIR:-/data/news_out}"
REFRESH_SECONDS="${REFRESH_SECONDS:-86400}"
SCHEDULE_TIMES="${SCHEDULE_TIMES:-}"
PORT="${PORT:-8080}"

mkdir -p "${OUT_DIR}"

build_once() {
  python3 /app/scripts/news_sync_server.py --config "${CONFIG_PATH}"
}

build_once

python3 -m http.server "${PORT}" --directory "${OUT_DIR}" &
SERVER_PID=$!

trap 'kill ${SERVER_PID}; exit 0' INT TERM

next_run_epoch() {
  local now next candidates t ts
  now=$(date +%s)
  next=$((now + REFRESH_SECONDS))
  if [[ -n "${SCHEDULE_TIMES}" ]]; then
    IFS=',' read -r -a candidates <<< "${SCHEDULE_TIMES}"
    for t in "${candidates[@]}"; do
      t="$(echo "$t" | xargs)"
      [[ -z "$t" ]] && continue
      ts=$(date -d "today ${t}" +%s || true)
      if [[ -n "$ts" && "$ts" -le "$now" ]]; then
        ts=$(date -d "tomorrow ${t}" +%s || true)
      fi
      if [[ -n "$ts" && "$ts" -lt "$next" ]]; then
        next=$ts
      fi
    done
  fi
  echo "$next"
}

while true; do
  now=$(date +%s)
  next=$(next_run_epoch)
  sleep_for=$((next - now))
  if [[ "$sleep_for" -gt 0 ]]; then
    sleep "$sleep_for"
  fi
  build_once || true
done
