#!/bin/bash
# Stream Finder — status. Run anytime to see the URLs + whether the servers are up.
# (Nothing needs to be started manually: launchd does that at boot.)
set -u
APP=/Users/marksparrow/testai/stream-finder
LAN_IP=$(ipconfig getifaddr en0 2>/dev/null || ipconfig getifaddr en1 2>/dev/null || echo 127.0.0.1)
DNSNAME=$(/usr/local/bin/tailscale status --json 2>/dev/null | /usr/bin/python3 -c 'import sys,json;print(json.load(sys.stdin)["Self"]["DNSName"].rstrip("."))' 2>/dev/null)
TS_IP=$(/usr/local/bin/tailscale ip -4 2>/dev/null)
TS_UP=$(/usr/local/bin/tailscale status 2>/dev/null | grep -q "$TS_IP" && echo yes || echo no)

code(){ /usr/bin/curl -sk -o /dev/null --max-time 4 -w "%{http_code}" "$1" 2>/dev/null; }
badge(){ [ "$1" = "200" ] && echo "UP  " || echo "DOWN"; }

C4443=$(code "https://127.0.0.1:4443/api/meta")
C8443=$(code "https://$LAN_IP:8443/api/meta")

echo "=============================================="
echo "  Stream Finder — status"
echo "=============================================="
echo
echo "  ANYWHERE (mobile, on your Tailscale):"
echo "    https://$DNSNAME:10000   $(badge $C4443)  [Tailscale: $TS_UP]"
echo
echo "  FAMILY (same home Wi‑Fi):"
echo "    https://$LAN_IP:8443   $(badge $C8443)"
echo
echo "  ON THIS MAC:"
echo "    https://127.0.0.1:8443   $(badge $(code "https://127.0.0.1:8443/api/meta"))"
echo
if [ "$C4443" != "200" ] || [ "$C8443" != "200" ]; then
  echo "  Something is down? Try:"
  echo "    launchctl kickstart -k gui/\$(id -u)/com.masparrow.stream-finder"
  echo "    launchctl kickstart -k gui/\$(id -u)/com.masparrow.stream-finder.lan"
  echo
fi
echo "  (No start script needed — launchd runs everything at boot.)"
echo "=============================================="
