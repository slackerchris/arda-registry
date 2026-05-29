#!/usr/bin/env sh
set -e

mkdir -p /app/output/logs /app/output/state

if [ "$(id -u)" = "0" ]; then
  chown -R arda:arda /app/output /app/data 2>/dev/null || true

  if [ -n "${MAFL_OUTPUT_PATH:-}" ]; then
    mkdir -p "$(dirname "$MAFL_OUTPUT_PATH")" 2>/dev/null || true
    chown arda:arda "$(dirname "$MAFL_OUTPUT_PATH")" 2>/dev/null || true
  fi

  if [ -n "${MAFL_NAS_PATH:-}" ]; then
    mkdir -p "$(dirname "$MAFL_NAS_PATH")" 2>/dev/null || true
    chown arda:arda "$(dirname "$MAFL_NAS_PATH")" 2>/dev/null || true
  fi

  exec gosu arda "$@"
fi

exec "$@"
