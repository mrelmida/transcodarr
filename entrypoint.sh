#!/bin/bash
set -e

mkdir -p /app/logs

# Ensure .env is writable (fixes Docker volume permission issues)
if [ -f "/app/.env" ]; then
    chmod 666 /app/.env 2>/dev/null || true
fi
if [ -n "$ENV_FILE_PATH" ] && [ -f "$ENV_FILE_PATH" ]; then
    chmod 666 "$ENV_FILE_PATH" 2>/dev/null || true
fi

exec uvicorn web.app:app --host 0.0.0.0 --port 5025 --workers 1 --timeout-keep-alive 75
