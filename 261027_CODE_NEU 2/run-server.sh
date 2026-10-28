#!/bin/bash
cd "$(dirname "$0")"
PY="./.venv/bin/python3"
[ -x "$PY" ] || PY=python3
LOG=/tmp/twitch-null-server.log
exec >>"$LOG" 2>&1
echo "=== $(date) starting ==="
exec "$PY" server.py
