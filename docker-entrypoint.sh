#!/bin/sh
set -eu

export HOME=/home/appuser
export DENO_DIR=/home/appuser/.cache/deno

mkdir -p /app/data /tmp/media_saver_bot "$DENO_DIR"
chown -R appuser:appuser /app/data /tmp/media_saver_bot /home/appuser

exec gosu appuser "$@"
