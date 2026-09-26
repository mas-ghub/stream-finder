#!/usr/bin/env bash
# Stream Finder — start the whole app (backend serves the frontend).
# Binds to 0.0.0.0 so your phone / other devices on the Wi-Fi can reach it.
set -e
cd "$(dirname "$0")/backend"
[ -d .venv ] || uv venv -q
uv pip install -q fastapi "uvicorn[standard]" httpx pydantic pydantic-settings 2>/dev/null
LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo "127.0.0.1")
echo "Starting Stream Finder → http://$LAN_IP:8030  (Mac: http://127.0.0.1:8030)"
pkill -f "uvicorn app.main" 2>/dev/null || true
sleep 0.5
exec .venv/bin/python -m uvicorn app.main:app --port 8030 --host 0.0.0.0
