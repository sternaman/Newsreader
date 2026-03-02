#!/usr/bin/env bash
set -euo pipefail

CONFIG_PATH="${CONFIG_PATH:-/config/news_sources.json}"
OUT_DIR="${OUT_DIR:-/data/news_out}"
REFRESH_SECONDS="${REFRESH_SECONDS:-86400}"
SCHEDULE_TIMES="${SCHEDULE_TIMES:-}"
PORT="${PORT:-8080}"
LOCK_DIR="${LOCK_DIR:-/tmp/news_sync_lock}"
LOCK_PID_FILE="${LOCK_DIR}/pid"
SYNC_TIMEOUT_SECONDS="${SYNC_TIMEOUT_SECONDS:-3300}"

mkdir -p "${OUT_DIR}"

release_lock() {
  rm -f "${LOCK_PID_FILE}" 2>/dev/null || true
  rmdir "${LOCK_DIR}" 2>/dev/null || true
}

clear_stale_lock() {
  if [[ ! -d "${LOCK_DIR}" ]]; then
    return 0
  fi

  local lock_pid=""
  if [[ -f "${LOCK_PID_FILE}" ]]; then
    lock_pid="$(cat "${LOCK_PID_FILE}" 2>/dev/null || true)"
  fi

  if [[ -n "${lock_pid}" ]] && kill -0 "${lock_pid}" 2>/dev/null; then
    return 0
  fi

  echo "Found stale news sync lock; clearing."
  rm -rf "${LOCK_DIR}"
}

run_sync() {
  if [[ "${SYNC_TIMEOUT_SECONDS}" -gt 0 ]]; then
    timeout "${SYNC_TIMEOUT_SECONDS}" python3 /app/scripts/news_sync_server.py --config "${CONFIG_PATH}"
  else
    python3 /app/scripts/news_sync_server.py --config "${CONFIG_PATH}"
  fi
}

build_once() {
  clear_stale_lock
  if mkdir "${LOCK_DIR}" 2>/dev/null; then
    (
      echo "${BASHPID:-$$}" > "${LOCK_PID_FILE}"
      trap 'release_lock' EXIT INT TERM
      run_sync
    )
  else
    echo "News sync already running; skipping."
  fi
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
