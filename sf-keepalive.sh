#!/bin/bash
# Stream Finder — keep the free-tier Render backend warm.
#
# Render's free tier sleeps after ~15 min idle and takes 1-2 min to wake. This
# loop pings /api/meta every 20 min (just inside the sleep window) so the cloud
# backend stays awake. It only runs while THIS Mac is booted (launchd), which is
# exactly the "when it's on" behaviour you asked for — when the Mac is off, the
# cloud sleeps and the app behaves exactly as it always has (wake overlay etc.).
#
# It is a standalone background loop: it does NOT touch the app, the frontend,
# or the local backend. It just sends a tiny GET and sleeps.
set -u
URL="https://stream-finder-api.onrender.com/api/meta"
INTERVAL="${SF_KEEPALIVE_INTERVAL:-1200}"   # seconds between pings (20 min)
LOG="/tmp/sf_keepalive.log"
CURL="/usr/bin/curl"

log(){ printf '%s keepalive: %s\n' "$(date '+%Y-%m-%d %H:%M:%S')" "$1" >> "$LOG"; }

log "started (interval ${INTERVAL}s) url=$URL"
while true; do
  if "$CURL" -sf --max-time 90 -o /dev/null "$URL" 2>/dev/null; then
    log "ping ok"
  else
    # Cold start can take 60-120s; retry a couple of times before giving up
    # on this cycle. A total failure just gets logged; the next cycle retries.
    ok=0
    for i in 1 2 3; do
      sleep 15
      if "$CURL" -sf --max-time 90 -o /dev/null "$URL" 2>/dev/null; then ok=1; break; fi
    done
    [ "$ok" = 1 ] && log "ping ok (after retry)" || log "ping FAILED (backend unreachable / sleeping?)"
  fi
  sleep "$INTERVAL"
done
