#!/usr/bin/env bash
# Stream Finder — HTTPS via a Tailscale-issued (Let's Encrypt) certificate.
#
# Serves the app on https://marks-macbook-pro.tail0003aa.ts.net  (port 443).
# Reachable from any device on your Tailscale tailnet — phone, office, cafe —
# as long as it's signed into the same account, even when off your home Wi-Fi.
#
# Usage:  ./start-https.sh
set -e
cd "$(dirname "$0")/backend"

SOCK="${TAILSCALE_SOCKET:-/tmp/tailscale-state/tailscaled.sock}"
export TAILSCALE_SOCKET="$SOCK"
STATE_DIR="$(dirname "$SOCK")"
mkdir -p "$STATE_DIR"

# 1) Make sure tailscaled is running (this CLI drives it).
if ! tailscale status >/dev/null 2>&1; then
  echo "tailscaled not running — starting it (state in $STATE_DIR)…"
  mkdir -p "$STATE_DIR"
  nohup tailscaled --state="$STATE_DIR/tailscaled.state" \
        --socket="$SOCK" --tLogFile="$STATE_DIR/tailscaled.log" \
        > "$STATE_DIR/tailscaled.out" 2>&1 &
  sleep 5
fi
tailscale status >/dev/null 2>&1 || { echo "ERROR: tailscaled is not up. Run: tailscale up"; exit 1; }

# 2) Work out this machine's MagicDNS name.
HOSTNAME_BASE="$(tailscale status --json 2>/dev/null | python3 -c 'import sys,json;print(json.load(sys.stdin)["Self"]["DNSName"].split(".")[0])')"
DNSNAME="${HOSTNAME_BASE}.tail0003aa.ts.net"
# Keep the cert/key OUT of backend/ (so they're never accidentally committed).
CRT="$STATE_DIR/$DNSNAME.crt"
KEY="$STATE_DIR/$DNSNAME.key"

# 3) Refresh the Let's Encrypt cert, written to the state dir.
echo "Refreshing TLS cert for $DNSNAME …"
( cd "$STATE_DIR" && tailscale cert "$DNSNAME" --cert-file "$DNSNAME.crt" --key-file "$DNSNAME.key" ) >/dev/null 2>&1 \
  || ( cd "$STATE_DIR" && tailscale cert "$DNSNAME" ) >/dev/null

# 4) Serve over HTTPS on 4443. A separate port (not 443) so it can coexist with
#    the business app on the same ts.net host without any path collision.
#    Tailscale signs a Let's Encrypt cert for <host>:4443 automatically (any port
#    on a *.ts.net name works), so the URL is just the origin on that port.
#    ASGI_PREFIX=/stream-finder strips the prefix so /api/* routes resolve; the
#    app's root_path matches for docs. (0.0.0.0 = also on plain home Wi-Fi.)
SF_PORT="${SF_PORT:-4443}"
export SF_HOST=0.0.0.0 SF_PORT SF_PREFIX="/stream-finder" ASGI_PREFIX="/stream-finder"
echo
echo "============================================================"
echo "  Stream Finder (HTTPS)"
echo "    tailnet : https://$DNSNAME:$SF_PORT"
echo "    local   : https://127.0.0.1:$SF_PORT"
echo "============================================================"
echo
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port "$SF_PORT" \
  --root-path "/stream-finder" \
  --ssl-certfile "$CRT" --ssl-keyfile "$KEY"
