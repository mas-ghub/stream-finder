#!/usr/bin/env bash
# Quick end-to-end smoke test for the Stream Finder backend.
set -e
cd "$(dirname "$0")"
[ -d .venv ] || uv venv -q
uv pip install -q fastapi "uvicorn[standard]" httpx pydantic pydantic-settings 2>/dev/null

pkill -f "uvicorn app.main" 2>/dev/null || true
sleep 1
.venv/bin/python -m uvicorn app.main:app --port 8030 --log-level critical >/dev/null 2>&1 &
PID=$!
trap "kill $PID 2>/dev/null" EXIT
for i in $(seq 1 30); do curl -s -o /dev/null http://127.0.0.1:8030/api/meta && break; sleep 0.5; done

B=http://127.0.0.1:8030
echo "1) meta:";       curl -s "$B/api/meta" | python3 -c "import sys,json;[print('   ',s['key'],s['name'],'configured' if s['configured'] else 'needs-key') for s in json.load(sys.stdin)['sources']]"
echo "2) search shows (horror):"; curl -s "$B/api/search?kind=show&genres=Horror&limit=3" | python3 -c "import sys,json;d=json.load(sys.stdin);print('   ',d['count'],'->',[r['title'] for r in d['results']])"
echo "3) search name 'office':"; curl -s "$B/api/search?q=office&kind=show&limit=3" | python3 -c "import sys,json;d=json.load(sys.stdin);print('   ',d['count'],'->',[r['title'] for r in d['results']])"
echo "4) enrich Inception (RT):"; curl -s -X POST "$B/api/enrich" -H "Content-Type: application/json" -d '{"title":"Inception","kind":"movie","year":2010}' | python3 -c "import sys,json;d=json.load(sys.stdin);r=d['ratings'];print('   rt',r.get('rt_tomatometer'),'aud',r.get('rt_audience'),'cast',len(d.get('cast',[])),'trailer',bool(d.get('trailer_url')))"
echo "5) root serves frontend:"; curl -s "$B/" | grep -o "<title>[^<]*</title>" || echo "    (frontend not served)"
echo "OK — all checks ran. App at http://127.0.0.1:8030"
