#!/bin/bash
# Stream Finder — server supervisor (run under launchd, foreground).
# One instance = one app server; launchd KeepAlive restarts it if it dies.
# Usage: sf-start.sh 4443   (Tailscale "anywhere" route)
#        sf-start.sh 8443   (family home-Wi-Fi front door)
set -u
PORT="${1:?usage: sf-start.sh <port>}"
export TAILSCALE_SOCKET="${TAILSCALE_SOCKET:-/Library/Tailscale/tailscaled.sock}"
APP=/Users/marksparrow/testai/stream-finder
cd "$APP/backend" || exit 1

TS=("/usr/local/bin/tailscale")
up(){ /usr/bin/curl -s -o /dev/null --max-time 3 "$1" 2>/dev/null; }

# Wait for Tailscale to be up (Tailscale.app starts its own root daemon on login).
for i in $(seq 1 30); do "${TS[@]}" status >/dev/null 2>&1 && break; sleep 2; done
"${TS[@]}" status >/dev/null 2>&1 || open -a Tailscale || true

# Free the port if a stale/manual instance is holding it.
/usr/bin/pkill -f "uvicorn app.main:app --host 0.0.0.0 --port $PORT" 2>/dev/null || true
sleep 1

LOG="/tmp/sf_port${PORT}.log"
CERTS="$APP/certs"
UV="$APP/backend/.venv/bin/python"

if [ "$PORT" = "4443" ]; then
  DNSNAME="$("${TS[@]}" status --json 2>/dev/null | /usr/bin/python3 -c 'import sys,json;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null)"
  STATE="/tmp/tailscale-state"
  mkdir -p "$STATE"
  # Refresh the Let's Encrypt cert (cheap; also self-heals an expired cert).
  ( cd "$STATE" && "${TS[@]}" cert "$DNSNAME" --cert-file "$DNSNAME.crt" --key-file "$DNSNAME.key" >/dev/null 2>&1 ) || true
  exec "$UV" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" \
    --root-path /stream-finder --log-level warning \
    --ssl-certfile "$STATE/$DNSNAME.crt" --ssl-keyfile "$STATE/$DNSNAME.key" >> "$LOG" 2>&1
else
  exec "$UV" -m uvicorn app.main:app --host 0.0.0.0 --port "$PORT" --log-level warning \
    --ssl-certfile "$CERTS/server.crt" --ssl-keyfile "$CERTS/server.key" >> "$LOG" 2>&1
fi
