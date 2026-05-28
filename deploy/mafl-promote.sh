#!/usr/bin/env bash
set -euo pipefail

REQUEST_FILE="${MAFL_DEPLOY_REQUEST:-/mnt/downloads/mafl/config.deploy.yml}"
SOURCE_FILE="${MAFL_SOURCE_FILE:-/mnt/downloads/mafl/config.yml}"
DEST_FILE="${MAFL_DEST_FILE:-/docker/mafl/config.yml}"
RESTART_COMMAND="${MAFL_RESTART_COMMAND:-docker restart mafl}"

if [[ ! -f "$SOURCE_FILE" ]]; then
  echo "Mafl source config is missing: $SOURCE_FILE" >&2
  exit 1
fi

mkdir -p "$(dirname "$DEST_FILE")"

if [[ -f "$DEST_FILE" ]]; then
  cp "$DEST_FILE" "$DEST_FILE.$(date +%Y%m%d_%H%M%S).bak"
fi

tmp_file="$(mktemp "$(dirname "$DEST_FILE")/.config.yml.XXXXXX")"
cp "$SOURCE_FILE" "$tmp_file"
chmod 0644 "$tmp_file"
mv "$tmp_file" "$DEST_FILE"

if [[ -n "$RESTART_COMMAND" ]]; then
  bash -lc "$RESTART_COMMAND"
fi

if [[ -f "$REQUEST_FILE" ]]; then
  mv "$REQUEST_FILE" "$REQUEST_FILE.done"
fi

echo "Promoted Mafl config from $SOURCE_FILE to $DEST_FILE"
