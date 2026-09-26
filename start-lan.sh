#!/usr/bin/env bash
# Stream Finder — start the app reachable from your phone / any device on the
# same Wi-Fi network. Prints the URL to open on your iPhone.
set -e
cd "$(dirname "$0")/backend"

[ -d .venv ] || uv venv -q
uv pip install -q fastapi "uvicorn[standard]" httpx pydantic pydantic-settings 2>/dev/null || true

# Free the port if a previous instance is running
pkill -f "uvicorn app.main" 2>/dev/null || true
sleep 0.5

PORT="${SF_PORT:-8030}"
HOST="${SF_HOST:-0.0.0.0}"

echo "Starting Stream Finder on port $PORT ..."
SF_HOST="$HOST" SF_PORT="$PORT" .venv/bin/python -m uvicorn app.main:app --host "$HOST" --port "$PORT" --log-level warning &
PID=$!

# Give it a moment to boot, then print the phone-friendly URL.
sleep 3
LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || hostname -I 2>/dev/null | awk '{print $1}' || echo "127.0.0.1")

echo ""
echo "=============================================="
echo "  Stream Finder is running."
echo ""
echo "  On this Mac:   http://127.0.0.1:$PORT"
echo "  On your iPhone (same Wi-Fi):"
echo "     http://$LAN_IP:$PORT"
echo ""
echo "  Tip: open it once, then use the share menu > 'Add to Home Screen'"
echo "       for a full-screen app-like experience."
echo ""
echo "  Press Ctrl+C here to stop it."
echo "=============================================="

# Follow the server logs; Ctrl+C stops everything.
trap "kill $PID 2>/dev/null" INT TERM
wait $PID
