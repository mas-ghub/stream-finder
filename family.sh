#!/usr/bin/env bash
# Stream Finder — one command to let the family use it.
#
#   ./family.sh
#
# Starts the backend (HTTP 8030 + HTTPS 8443) on your LAN and prints the addresses.
# Your family then just opens the GitHub Pages URL while they're on your home Wi-Fi
# (no app to install, no Tailscale). HTTPS is required because Safari blocks a secure
# GitHub Pages page from fetching a plain-http Mac ("mixed content").
set -e
cd "$(dirname "$0")"
cd backend

LAN_IP="$(ifconfig en0 2>/dev/null | /usr/bin/grep 'inet ' | /usr/bin/awk '{print $2}' | /usr/bin/head -1)"
[ -z "$LAN_IP" ] && LAN_IP="$(ifconfig en1 2>/dev/null | /usr/bin/grep 'inet ' | /usr/bin/awk '{print $2}' | /usr/bin/head -1)"
[ -z "$LAN_IP" ] && LAN_IP="127.0.0.1"

up(){ /usr/bin/curl -s -o /dev/null --max-time 3 "$1" 2>/dev/null && echo yes || echo no; }

# HTTP 8030 (direct / older clients)
if [ "$(up http://127.0.0.1:8030/api/meta)" != "yes" ]; then
  echo "Starting HTTP backend (8030)…"
  nohup .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8030 > /tmp/sf_http.log 2>&1 &
fi

# HTTPS 8443 (what the GitHub Pages site uses — needs a TRUSTED cert, or Safari
# blocks it as "self-signed" and the phone gets an instant network-error).
if [ "$(up -k https://127.0.0.1:8443/api/meta)" != "yes" ]; then
  if [ ! -f ../certs/server.crt ]; then
    echo "No CA-signed cert yet — generating a local CA + server cert (LAN IP $LAN_IP)…"
    mkdir -p ../certs
    /usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 3650 \
      -keyout ../certs/ca.key -out ../certs/ca.crt \
      -subj "/CN=StreamFinder-Home-CA" \
      -addext "basicConstraints=critical,CA:TRUE" -addext "keyUsage=critical,keyCertSign,cRLSign" >/dev/null 2>&1
    /usr/bin/openssl req -newkey rsa:2048 -nodes -keyout ../certs/server.key -out ../certs/server.csr \
      -subj "/CN=stream-finder.local" >/dev/null 2>&1
    /usr/bin/openssl x509 -req -in ../certs/server.csr -CA ../certs/ca.crt -CAkey ../certs/ca.key \
      -CAcreateserial -days 3650 -out ../certs/server.crt \
      -extfile <(printf "subjectAltName=IP:%s,DNS:stream-finder.local,DNS:localhost,IP:127.0.0.1\n" "$LAN_IP") >/dev/null 2>&1
    echo "  → Trust the CA once (one-time, needs your password):"
    echo "      sudo security add-trusted-cert -d -r trustRoot -k /Library/Keychains/System.keychain ../certs/ca.crt"
  fi
  echo "Starting HTTPS backend (8443) with the trusted CA-signed cert…"
  nohup .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8443 \
    --ssl-certfile ../certs/server.crt --ssl-keyfile ../certs/server.key > /tmp/sf_https.log 2>&1 &
fi

for i in $(seq 1 15); do
  [ "$(up -k https://127.0.0.1:8443/api/meta)" = "yes" ] && break
  sleep 1
done

HTTPS_OK="$(up -k https://127.0.0.1:8443/api/meta)"
[ "$HTTPS_OK" = "yes" ] && STATUS="✓ running (https)" || STATUS="✗ NOT running (check /tmp/sf_https.log)"

cat <<EOF

============================================================
  Stream Finder is $STATUS

  🌐  FAMILY opens this (any phone, no install, no Tailscale):
      https://mas-ghub.github.io/stream-finder/
      …while on your HOME Wi-Fi. It reaches the Mac over HTTPS
      automatically — no setup, no warnings.

      (First run on a new Mac: trust the local CA once —
      the command is printed above — then it's silent forever.)

  🖥️  Direct links (you, on the same Wi-Fi):
      https://$LAN_IP:8443/        (HTTPS — what the site uses)
      http://$LAN_IP:8030/         (HTTP — direct)

  📄  On this Mac:  https://127.0.0.1:8443/

  🔒  If a device says "This connection is not private",
      trust the CA once (needs your password):
        sudo security add-trusted-cert -d -r trustRoot \
          -k /Library/Keychains/System.keychain ~/testai/stream-finder/certs/ca.crt

  ⚠️  Away from home on mobile data? The Mac isn't
      reachable, so the site will say "come home".
============================================================

EOF
