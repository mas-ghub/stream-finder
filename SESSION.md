# Stream Finder — dev handover (SESSION)

Working notes so we can pick up where we left off. Not user-facing (that's README.md).
Last updated: the "Chrome local-network block → permission flow" session.

## FUTURE IDEA (user, not now): host without depending on the Mac staying on/awake/logged in (e.g. always-on hosting). Mac currently needs: plugged in, lid open, logged in. `sudo pmset -c sleep 0` applied (display sleep still 10 min).

## v1.44: cards show ONE of my services (green ✓) + a '+N' button (data-subs → showSubs popup: 'On your services' / 'Also on (not yours)' / rent-buy). Language badge on cards: backend Result.language (TMDB original_language), client Intl.DisplayNames. status.sh URL fixed to :10000. Backend restarted via launchctl kickstart. Cache v18.

## v1.43: render() rebuilds the whole page (innerHTML) — search box lost focus/keyboard when results landed or the debounced search fired. Now restores focus+caret after each render; typing debounce 350→900ms. Cache v18? (sf-shell-v17).

## v1.42: sw.js no longer intercepts /api/* (was cache-first for ALL same-origin GETs incl. searches → stale results, and iOS kills a SW holding a 20s+ fetch → spurious "can't reach" banner). Cache v16.

## v1.37-1.41: Reset-keys removed; year dropdowns labelled; default provider Netflix (empty saved pick => Netflix); silent search retry + theme-safe error banner; detail/actor/where-to-watch popups now use the outer-fixed-layer-scrolls pattern (same as Settings). SW cache v15.

## v1.36: settings modal — outer fixed layer is the scroller (min-h-full flex centre) so top/bottom (Save) are reachable on iOS; `.bg-accent` forced white text. SW cache v10.

## THIS SESSION (v1.35 — settings scroll, all-services default, latest-click-wins)
- Settings modal card had no max-height/overflow → couldn't scroll on iPhone. Now `max-h-[92vh] overflow-y-auto`.
- `doSearch()` used `if(busy) return` → clicking Horror during a slow search (~12s) was silently
  dropped. Now latest click wins (AbortController + `searchSeq`); nothing is ignored.
- New `lensChannels()`: default = ALL subscribed services (no manual selection); genres, years,
  ratings combine with it. Removed the code that switched the services filter off when a
  mood/year was chosen. Text search still bypasses it. Boot now restores a partial saved pick.
- SW cache sf-shell-v9.

## THIS SESSION (iPhone fix — Funnel on :10000, coexists with Assessment Portal)
Why it failed: Funnel pointed at `http://127.0.0.1:8443` (that port is HTTPS → 502), and
the frontend's `onMac()` only recognised the ts.net host on port 4443, so on the public
URL it probed dead backends. Also `location.search.get(...)` threw (string, not
URLSearchParams) → fixed.
- Funnel: `tailscale funnel --bg --https=10000 https+insecure://127.0.0.1:4443` (persists in
  tailscaled). Funnel only allows public ports 443/8443/10000. **443 is deliberately left
  free** for `~/AssessmentPortal/mac/tunnel-go.sh` (runs `tailscale funnel 5080` = 443, and
  stop runs `funnel 5080 off`) — never put Stream Finder on 443. 8443 avoided: local LAN server uses it.
- Frontend v1.34 / SW cache sf-shell-v8: `onMac()` = hostname === TAILNET_HOST (any port);
  `MAC_TS` fallback = `...ts.net:10000`.
- **Family URL: `https://marks-macbook-pro.tail0003aa.ts.net:10000`** (verified via public DNS/IP: 200).
- Startup: app servers via launchd LaunchAgents (start at LOGIN, not power-on); Funnel config
  persists in Tailscale. Root boot daemon exists but is unverified. Reboot test NOT yet done —
  after reboot run `./status.sh`.
- The older sections below that mention `:4443` as the public URL are superseded.

## THIS SESSION (gate REMOVED — public no-setup URL + funnel via launchd)
User: "no passcode — give my wife the URL and it should just work; also you took away
the port number." Reverted all of it (v1.32):
- `backend/.env`: SF_ACCESS_TOKEN / SF_OWNER_PASSCODE deleted (passcode bCqFppzE is
  now void — ignore it).
- `main.py`: gate middleware, /api/login, key-hiding in _key_status all removed.
  `config.py`: access_token/owner_passcode removed.
- Frontend: gateScreen/ensureGate/ownerPrompt removed; `tmdbHeaders()` back to `{}`.
- **Funnel via launchd** (user: "when I reboot the mac it will just work, right?"):
  `~/Library/LaunchAgents/com.masparrow.stream-finder.funnel.plist` runs
  `tailscale funnel 4443` (KeepAlive, ThrottleInterval 30, log /tmp/sf_funnel.log).
  **PENDING USER SUDO:** the root boot daemon's inline script must also bootstrap
  `com.masparrow.stream-finder.funnel.plist` (see command in chat) — without it a
  pre-login reboot leaves Funnel down.
- **The shareable URL (no app, no passcode, no port magic):**
  `https://marks-macbook-pro.tail0003aa.ts.net:4443` — verified from the public
  internet: index 200, meta 200, search 200. (Funnel prints "port 443 at the bare
  ts.net host" in its banner, but the app's own cert is for :4443, so always use the
  :4443 URL; the no-port form would show a cert warning.)
- Honest trade-off in README: public URL = anyone who has it can browse + could
  overwrite the TMDB key in Settings (resets on restart; real keys in .env).
- README "Use it from anywhere" section rewritten to match (no setup for viewer).

## THIS SESSION (public access WITHOUT the Tailscale app — Funnel + backend gate)
User: iPhone can't open the ts.net:4443 address without the Tailscale app, but their
Assessment Portal works on the phone without it. **Why:** the Portal's
`mac/tunnel-start.sh` runs `tailscale funnel` (public internet exposure, no app needed).
NOTE: `~/AssessmentPortal` is NOT this app — its tunnel files were only read for
understanding, never modified.
User chose: public exposure, BUT "no one should be able to change anything or see
anything" (no keys, no settings changes).

**Access gate (backend-enforced — client-side bypass impossible):**
- `config.py`: `access_token` (SF_ACCESS_TOKEN) + `owner_passcode` (SF_OWNER_PASSCODE).
- `main.py` middleware: every `/api/*` needs header `X-SF-Token == SF_ACCESS_TOKEN`
  (401 otherwise); `POST /api/keys` additionally needs `X-SF-Owner == SF_OWNER_PASSCODE`.
  `/api/login` (POST {passcode}) is the only un-gated route: owner passcode in → app
  token out (hmac.compare_digest, no timing oracle). `GET /api/keys` under the gate
  returns `gate:true` with key state = null (never reveals which sources are configured).
- `backend/.env`: `SF_ACCESS_TOKEN=2e502f431ddcfd2d29dbb8f58bd1da14`,
  `SF_OWNER_PASSCODE=bCqFppzE` (owner credentials — stored on the Mac; tell the user
  the passcode is the key to the Settings/⚙️ page).
- Frontend (v1.31): `tmdbHeaders()` attaches X-SF-Token; boot calls `ensureGate()` —
  401 → full 🔐 login screen (passcode → token in localStorage `sf_gate_token`);
  ⚙️ Settings prompts for the owner passcode (`sf_owner_pass`) when gated and hides the
  key fields (shows "managed on the Mac, hidden in this public view").
- **Verified live** on :8443: stranger meta=401; +token=200; login: empty/wrong →
  bad-passcode, owner → token; POST keys w/o owner=401, with owner=200; search=200.
  Static (/, sw.js, manifest) stay open so the page can load the gate.
- **Funnel**: `tailscale funnel 4443` prints the URL but returned 502 + "No serve
  config" → **Funnel is not enabled for the device** (admin-console setting). The
  Portal's tunnel works only when the user is mid-run (their foreground `tailscale
  funnel` process); it's down now. **USER ACTION:** enable Funnel at
  https://login.tailscale.com/admin/funnel, then run `tailscale funnel 4443`
  (add to a start script for persistence, or rely on the app+server already running
  via launchd). Once Funnel is on, `https://marks-macbook-pro.tail0003aa.ts.net/`
  (no port) serves Stream Finder publicly, gated.
- Caveat accepted by user: public = internet can reach it; the gate + TMDB free-tier
  abuse risk remains (mitigated: keys hidden, writes need owner passcode).
- `:8443` (family) is ALSO gated now — family needs the passcode once on their phone.

## THIS SESSION (launchd auto-start — app comes up on boot, restarts on crash)
User: "yes, do the launchd auto-start; I know the Mac must be on, screen sleep is fine
(other app already works that way)."

**How it's wired now (verified live):**
- `sf-start.sh <port>` — rewritten as a **foreground supervisor** (launchd-ownable, no
  more detached nohup): waits for tailscaled, refreshes the ts.net Let's Encrypt cert,
  frees the port of stale instances, then `exec`s uvicorn. `4443` = Tailscale anywhere
  route; `8443` = family home-Wi‑Fi front door (`certs/server.crt`). Per-port logs:
  `/tmp/sf_port{4443,8443}.log`.
- **Two LaunchAgents** in `~/Library/LaunchAgents/` (both `KeepAlive=true`, `RunAtLoad`,
  `ThrottleInterval=15`, logs → `/tmp/sf_launchd.log`):
  - `com.masparrow.stream-finder.plist` → `sf-start.sh 4443`
  - `com.masparrow.stream-finder.lan.plist` → `sf-start.sh 8443`
- **Bug found + fixed:** `tailscale status --json` DNSName has a trailing dot
  (`…ts.net.`) → cert filename mismatch, uvicorn `FileNotFoundError`. Fixed with
  `.rstrip(".")`.
- **Verified:** both serve 200 (`/api/meta` on 4443 via 127.0.0.1 AND via the Tailscale
  IP with `--resolve` ts.net name; 8443 via 192.168.0.59). Kill-test: `launchctl
  kickstart -k` → launchd revived it, 200 again, sibling server untouched.
- **macOS 26 (Tahoe) gotcha:** user LaunchAgents don't start until first login, so a
  **root boot daemon** is needed: `/Library/LaunchDaemons/com.masparrow.stream-finder.boot.plist`
  retries `launchctl bootstrap gui/501` for both agents for up to 5 min after boot.
  (Agent bootstrap fails with 150 until login — expected, the loop handles it.)
  The agent agents were already booted in gui/501 this session.
- The old manual `./start-https.sh` 4443 process was replaced by the launchd one.
  `./start.sh` (8030) and `./start-lan.sh` are now redundant for normal use
  (sf-start.sh frees the port first if a stale instance lingers); kept for manual runs.
- Note: front-end `MAC_LAN` is now `https://192.168.0.59:8443` (not :8030 http); the
  family front door needs the self-signed `certs/ca.crt` trusted on phones (one-time).

## LATER SAME DAY (Pages retired; family front-door = the Mac)
User: "get rid of it on git pages". Done the safe way — the Pages repo's
`index.html` is now a **tombstone** ("Stream Finder moved") with two buttons:
`https://192.168.0.59:8443` (family, home Wi‑Fi, no setup) and the Tailscale
`:4443` address (owner, anywhere). Deleted sw/manifest/check from the repo.
v1.27 of the real app is in git history + the Mac's `frontend/`. Front door for
family = the Mac-served app. (Tombstone also kept locally at `pages-tombstone.html`.)
**Next open items:** launchd auto-start (tailscaled + both uvicorns on boot);
hosted backend if family-away-from-home ever matters.

## THIS SESSION (the REAL root cause of "it never works": browser private-network gating)
Symptom: user opening the Pages app **on the Mac** still got the failure; every past
attempt "had trouble".
**Root cause found (reproduced in fresh headless Chrome):** the Mac backend was
fine — **newer Chrome/Safari block a PUBLIC site (mas-ghub.github.io) from fetching a
device on the local network** unless the user allows "Local network" for that origin:
`Access to fetch ... blocked by CORS policy: Permission was denied ... local address space`.
This is a browser privacy gate, NOT a server bug. (Also: the backend processes had
died — `family.sh` nohup processes don't survive reboots.)
**Fixes shipped (v1.26, deployed to Pages, verified in fresh Chrome):**
- `resolveBackend()` records `blocked` (TypeError on a local-addr candidate) +
  `isLocalAddr()`; `localNetPermissionNeeded()` drives a dedicated 🔒 error card:
  "Your browser is blocking local network access" with the address-bar → Site
  settings → Local network → Allow instructions, a **🔓 Allow button**
  (`navigator.permissions.query/request({name:'local-network'})`, user gesture) and
  a link to a new **`check.html`** helper page that asks the permission and reloads.
  Manual alert fallback when the API is unsupported. (Headless Chrome 154 auto-grants
  in the prompt; real Chrome shows its native prompt; Safari = manual steps.)
- Boot error now classified `kind:'network'`; copy no longer says "run ./family.sh"
  when the actual cause is the browser block.
- **Backend bug fixed** (`main.py /api/diag`): body parsed manually via `Request`
  instead of `payload: dict` — the old route 422'd (silently discarding) whenever
  the diag body failed FastAPI validation. Diags now actually arrive.
- Relative shell paths (manifest/icons/sw, `./`) so the repo-subdir Pages deploy
  resolves them (was 404 on `/manifest.webmanifest`); SW cache `sf-shell-v5`.
**Verified live (fresh profiles, v1.26 deployed):**
- Pages site on Mac → clean 🔒 card with Allow button (was: misleading error).
- `https://192.168.0.59:8443/` direct → works fully (200 results, no errors).
- `https://127.0.0.1:8443/` → works fully (same-origin).
**One-time per browser:** allow Local network for mas-ghub.github.io (or use the
direct address `https://192.168.0.59:8443` which never needs it).
**Still open:** no launchd auto-start (backend dies on reboot/sleep; `./family.sh`
relaunches). "Away from home" still needs the Mac on (Tailscale `:4443` route works
from anywhere for the owner; family on Wi-Fi only).

## THIS SESSION (fix iPhone 404; backend self-heals; SW auto-updates)
Symptom: opening the Pages app on the iPhone → "Could not reach the backend … **404**".
**Root cause:** the phone had **remembered the Tailscale `:4443` backend** in `localStorage`
(from an earlier change) — but that server was **down**, so every fetch 404'd. The working
home-Wi‑Fi `:8030` was never tried because the stale value won.

**Fixes (frontend `index.html`):**
- **`resolveBackend()`**: tries candidate backends in order — `?backend=` → `localStorage['sf_backend']`
  → (onMac ? `''` : `MAC_LAN` → `MAC_TS`) — with a 4s `AbortSignal.timeout` each; a **404 is
  skipped** (wrong/stale address) so it falls through to the next; the first that returns
  `200` is **remembered** in `sf_backend`. Self-heals a stale Tailscale pointer back to the
  working home-Wi‑Fi Mac.
- **`fetchMeta()` + `doSearch()`**: on a 404/bad response they **re-resolve and retry once**
  (so a mid-session stale backend recovers without a reload).
- **boot** calls `resolveBackend()` before first use; the error popup now explains "data lives
  on the family Mac → be home on Wi‑Fi + Mac on, or run `./family.sh`".
- **Service worker**: cache bumped `sf-shell-v1 → v3` and boot now calls `reg.update()` on
  every load, so an **installed PWA** (phone home-screen icon) picks up the new `index.html`
  automatically — no manual cache clear.
- `family.sh` (one command: `./family.sh`) prints the Pages URL + the direct LAN address.
- **One-time for the affected phone:** hard-refresh/clear the app once to load v1.22
  (Safari: Share → *Add to Home Screen* → re-open, or clear site data). After that it's
  automatic.
- `APP_VERSION` → **1.22**; deployed to Pages + `sw.js` `sf-shell-v3` live-verified.

## Prior session (family opens the Pages URL, home Wi‑Fi, no install)

## THIS SESSION (Pages = no-install, home-Wi-Fi auto-detect)
User: "they [family] don't need to install Tailscale, they just need to access it." I had
previously told the family to install Tailscale — that was WRONG: a `ts.net` address is private
by design and *requires* the Tailscale app. The no-install path is the public **GitHub Pages**
site, with live data from the Mac.

- **New frontend backend logic** (replaces the old `DEFAULT_BACKEND`):
  - `MAC_LAN = http://192.168.0.59:8030` (home Wi‑Fi), `MAC_TS = https://…ts.net:4443` (owner,
    works anywhere, needs Tailscale).
  - `onMac()` → same-origin (`API=''`) when on the Mac (`127.0.0.1`/`localhost` or the `:4443`
    ts.net host). Otherwise a fresh phone defaults to **`MAC_LAN`** (home Wi‑Fi) and that gets
    persisted; a working backend is remembered in `localStorage['sf_backend']` so revisits just
    work. `?backend=` still overrides.
  - **`doSearch`**: on a successful fetch, remembers the backend that worked; on failure sets
    `window.__backendDown` → header shows a clear notice: *"It lives on the family Mac — you can
    use it when you're home on the Wi‑Fi and the Mac is on → Retry."* (No Tailscale mentioned.)
  - **CORS**: backend sends `Access-Control-Allow-Origin: *` (verified on preflight + response),
    so the Pages origin → Mac backend cross-origin fetch works.
- **Settings → Backend** field: placeholder + hint now say "auto (home Wi‑Fi)", and offer the
  Tailscale address as the "works from anywhere" option (for the owner).
- **Current running config**: `./start.sh` → **HTTP 8030** on the LAN (the family home-Wi‑Fi
  path). The Mac is at **192.168.0.59** (en0). (The `:4443` HTTPS server is *stopped* for now;
  `./start-https.sh` brings it back if you want the owner's anywhere-access.)
- **Verified live**: Pages copy (v1.20) deploys; CORS open; `192.168.0.59:8030` reachable (200)
  from the LAN; `.env` self-sufficiency holds (no key headers needed).
- `APP_VERSION` → **1.20**.

**Family flow (no install):** open `https://mas-ghub.github.io/stream-finder/` **while at home
on the Wi‑Fi** (Mac running `./start.sh`). Away on mobile data → the "come home / turn on the
Mac" notice. This is the inherent limit of a home-Mac backend; the only way to be "up all the
time, anywhere, no install" would be a public cloud host (offered earlier, declined for now).

## Prior session (namespace Stream Finder on :4443 alongside the business app)

## THIS SESSION (coexist with the business app on the same ts.net host)
User: the Mac's ts.net host is **shared with the business app**; Stream Finder needs its own
address so they don't get mixed up. Since Tailscale can only bind **one app per port** of a
`ts.net` host, the collision-free answer is a **separate port**.

- **Stream Finder now serves on Tailscale `:4443`** (was 443, which the business app owns).
  Tailscale auto-signs a Let's Encrypt cert for `<host>:4443` (any port on a `*.ts.net` name
  works), so the address is simply **`https://marks-macbook-pro.tail0003aa.ts.net:4443`** —
  origin, no path, unambiguous, no collision with the business app on :443.
- **`start-https.sh`**: `SF_PORT` defaults to **4443**; sets `SF_PREFIX`+`ASGI_PREFIX=
  /stream-finder` + `--root-path /stream-finder`. **`ASGI_PREFIX` strips the prefix** (verified:
  `GET /stream-finder/api/meta` → 200), so the app's `/api/*`, `/` etc. resolve at the origin.
  (Earlier confusion: I chased a 404 thinking the prefix broke it — it didn't; `ASGI_PREFIX`
  works, and a request is `API + '/api/...'` with `API` = the *bare origin*.)
- **Frontend `DEFAULT_BACKEND`** = `https://marks-macbook-pro.tail0003aa.ts.net:4443` (bare
  origin — NO `/stream-finder` suffix, because the API paths must be origin-rooted after the
  prefix strip). The in-app UI is at `…/stream-finder` (root_path adds it for docs); the
  *fetch* base is the origin.
- **Verified live** on `:4443` (NO key headers → `.env` self-sufficiency intact): `/` index 200,
  `/sw.js` 200, `/manifest` 200, `/api/meta` tmdb configured, search The Crown → netflix.
- **TLS cert/key** now written to `/tmp/tailscale-state/` (not `backend/`) — can't be committed.
- `APP_VERSION` → **1.19**. Redeployed to Pages (its `DEFAULT_BACKEND` now `…:4443`).

**To reach it:**
- Mac / family / anywhere: **`https://marks-macbook-pro.tail0003aa.ts.net:4443`** (phone needs
  Tailscale signed in) — *and* the Mac running `./start-https.sh`.
- Pages front door: `https://mas-ghub.github.io/stream-finder/` (auto-targets the `:4443` origin).
- Business app keeps `:443` untouched.

## THIS SESSION (backend is now self-sufficient on TMDB keys)
User: "make sure the backend always uses my v4/whatever it needs, so we don't have to
worry about keys in the web app." Done — the backend now **always** uses the keys in
`backend/.env`; the web app never supplies or stores them for requests.

- **Middleware** (`main.py inject_key`): a frontend-supplied key is honoured **only if
  non-empty** (empty headers are ignored, so a stale/blank browser store can never clear the
  `.env` keys). In practice the frontend now sends **no** key headers at all, so `.env` always
  wins.
- **`meta`**: TMDB is reported "configured" if **either** key is present (`tmdb_key()` OR
  `tmdb_v4_key()`). Previously it checked only the v3 key, so a v4-only `.env` would
  incorrectly show the "add key" banner even though v4 powers search + details.
- **Frontend `tmdbHeaders()`** → now returns `{}` (sends nothing). Keys in `localStorage` are
  only a *convenience* for the in-app Settings; they're never used for requests.
- **Settings key boxes** are now **read-only + masked** when the backend has a key ("✓ Loaded
  from the Mac — used automatically"), and only editable to *add* a key to the Mac if one is
  missing. Save only POSTs a key if the user typed a NEW one (compares against the masked
  value). Save no longer writes keys into browser `localStorage`.
- **Verified live over the Tailscale address, NO key headers** (proves `.env` self-sufficiency):
  `/api/meta` → tmdb configured; search Bridgerton/The Crown returns results; `enrich` returns
  trailer + Netflix providers + cast (v4 all from `.env`).
- `APP_VERSION` → **1.17**. (Redeployed to Pages; also update the local `start.sh`/HTTPS app
  automatically since it's the same `frontend/index.html`.)

## Prior session (GitHub Pages deploy + Tailscale default backend)

## THIS SESSION (GitHub Pages deploy — always-on public frontend)
User wanted the app "up all the time". Solution: **GitHub Pages** hosting the **static
frontend only** (no backend, no keys) at a free, always-on, HTTPS address.

- **New public repo `mas-ghub/stream-finder`** → **`https://mas-ghub.github.io/stream-finder/`**
  (Pages on `main` + `/`, `build_type: workflow`, HTTPS enforced). **Deployed + verified live**
  (index + sw.js → 200, v1.15). Workflow: `.github/workflows/pages.yml` uploads `./` (the whole
  repo *is* the frontend) via `upload-pages-artifact` → `deploy-pages`.
- Repo contains **only**: `index.html`, `sw.js`, `manifest.webmanifest`, `icon.svg`,
  `README.md`, `.gitignore` (excludes `.env`/`*.key`/`*.crt`/`.venv`/`__pycache__`). **No
  backend, no API keys** — verified clean before push. `gh` is logged in as `mas-ghub`
  (scopes gist/read:org/repo/workflow); `gh repo create --public` used.
- **How the Pages copy gets live data:** the frontend's `API` base is now overridable via
  `localStorage['sf_backend']` or `?backend=`. On the public site (not :8030/localhost) it
  defaults to `http://127.0.0.1:8030` (works on the Mac) — a phone must set **Settings →
  Backend** to `http://192.168.0.59:8030` (the Mac's LAN IP) and have the Mac's `./start.sh`
  running + same network. If unreachable, the page shows a clear **"Can't reach the backend"**
  banner (`window.__backendDown`) with those steps instead of silently showing nothing.
  - **Caveat:** a phone on **cell data** (not home Wi-Fi) can't reach `192.168.0.59` — for that,
    the **Tailscale** route (`https://marks-macbook-pro.tail0003aa.ts.net`) is the way. The Pages
    site is the always-on *UI*; live data still needs the Mac on.
- **Also in this session:** TLS cert/key files were accidentally written to `backend/`
  (`marks-macbook-pro.tail0003aa.ts.net.{crt,key}`) by `tailscale cert` — they're gitignored /
  kept out of the public repo.
- `APP_VERSION` → **1.15**.

## THIS SESSION (HTTPS via Tailscale — Let's Encrypt cert, tailnet-reachable)
User wanted real HTTPS so the PWA installs reliably. Chose **Tailscale** (already installed;
account `masparrow70@`). It's private + works over the internet, but the phone must be on the
same tailnet.

- Tailscale is **up** this session: node `marks-macbook-pro`, Tailscale IP **100.86.118.20**,
  MagicDNS name **`marks-macbook-pro.tail0003aa.ts.net`** (suffix `tail0003aa.ts.net`).
  `tailscale cert` **works** (your tailnet has a domain configured) → it issued a valid
  **Let's Encrypt** cert (CN = the ts.net name, 90-day validity).
- **New `start-https.sh`** (run from the repo root): ensures `tailscaled` is up (user-level
  state in `/tmp/tailscale-state/`), refreshes the LE cert, and runs uvicorn on **port 443**
  with `--ssl-certfile/--ssl-keyfile`. Binds `0.0.0.0` so it also works on home Wi-Fi via the
  LAN IP. The old HTTP `start.sh` (port 8030) is still there; only **one** should run at a time.
- **Verified live:** `curl https://marks-macbook-pro.tail0003aa.ts.net/api/meta` → **HTTP 200,
  TLS-verify 0** (cert valid); `/` serves the app. `ts.net` resolves to 100.86.118.20 via
  Tailscale DNS (100.100.100.100).
- **How the user reaches it:**
  1. On the **phone**: install the **Tailscale** app (App Store) and sign in to **masparrow70@**.
  2. Open **`https://marks-macbook-pro.tail0003aa.ts.net`** — real, valid HTTPS (PWA install +
     offline now work properly). Works from anywhere, not just home Wi-Fi.
  3. To install the PWA: Safari → Share → Add to Home Screen (or the in-app **⬇️ Install**).
- **Caveats:** `tailscaled` is running in the **background** (`/tmp/tailscale-state/`) — on a
  reboot, re-run `./start-https.sh` (it restarts tailscaled automatically). The ts.net name is
  only reachable by tailnet members; for *public* (non-Tailscale) access you'd need a real
  domain (named Cloudflare Tunnel).
- `APP_VERSION` → **1.14**.

## Prior session (make it a PWA — installable + offline shell)

## THIS SESSION (make Stream Finder a PWA)
It *can* be a PWA — it was just missing the two required pieces: a **web app manifest** and a
**service worker** (for install + offline). Added both, plus an in-app **Install** button.

- `frontend/manifest.webmanifest` — name/short_name, `display:standalone`, portrait, theme
  `#e50914` + ink bg, `/` start_url+scope, and `/icon.svg` (any + maskable).
- `frontend/icon.svg` — 512 red rounded-square play-button logo (also used as favicon +
  apple-touch-icon; the old inline data-URI favicon was replaced with `/icon.svg`).
- `frontend/sw.js` — caches the **app shell** (`/`, manifest, icon, sw) only. Navigations =
  network-first with offline fallback; same-origin assets = cache-first. **Deliberately does
  NOT intercept cross-origin** requests (TMDB/RT/Tailwind CDN) so live data is always fresh —
  offline = the UI shell loads, a live search still needs the network. `sf-shell-v1` cache,
  `skipWaiting` + `clients.claim`.
- **Backend routes** (`main.py`): `GET /manifest.webmanifest`, `/sw.js`, `/icon.svg` serve the
  files from `frontend/` with the correct MIME types (`application/manifest+json`, etc.).
- **HTML head**: `<link rel="manifest">`, apple-mobile meta tags, theme-color, real icon.
  **Install button** in the header (`#install`) → `tryInstall()`: uses the
  `beforeinstallprompt` `prompt()` where supported (Chrome/Android/desktop), and on **iPhone
  Safari** (no such event) shows the "Share → Add to Home Screen" steps. SW registered on boot
  (http/https only).
- Verified live: all four routes 200 with correct content-types; manifest valid JSON; all JS +
  SW pass `node --check`. `APP_VERSION` → **1.14**.

**One honest caveat:** PWA "install" works best on **HTTPS**. Over plain **HTTP** on the LAN
(`http://192.168.0.59:8030`), Chrome still allows it but the offline SW + install prompt is
less reliable. If you want a guaranteed install + offline on the phone, run it behind **HTTPS**
(e.g. `mkcert` + a reverse proxy, or a `caddy` `tls internal` wrapper) — offer, not assumed.

## Prior session (stop rent/buy STORE channels mapping onto your subscription channels)
User reported two symptoms, **one root cause**:
1. **"Apple TV / Amazon Channel — that doesn't exist!"** + rent/buy entries for Apple/Prime/Sky
   were leaking into the *subscription* channels (`apple`, `prime`, `sky`).
2. **Free still wrong:** a title whose only Apple/Prime/Sky entries are *rent/buy* (e.g.
   "Apple TV Rent") was being counted as "free via your Apple/Prime sub".
3. **Selected channel not visible on the card** (but visible when you open the item).

**Root cause:** TMDB numeric ids collapse *store* providers onto the *subscription* channel:
`id 2` "Apple TV Store"→`apple`, `id 10` "Amazon Video"→`prime`, `id 130` "Sky Store"→`sky`
(see `ID_TO_CHANNEL` + `_EXTRA` in `channels.py`). So a rent/buy "Apple TV Store" entry got
`channel=apple` = your Apple sub, and "Amazon Video" rent → `channel=prime`.

**Fixes:**
- `channels.py channel_for()`: added a **store guard** — a provider whose *name* contains
  ` store` / `itunes` / `amazon video` / `amazon channel` returns **channel=None** (untracked),
  **before** the numeric-id mapping. So rent/buy Apple/Prime/Sky entries no longer carry your
  sub's channel. (`_EXTRA` aliases for those names also removed; substring fallback left —
  the guard runs first.)
- Free rule (backend `search()` + frontend `classify`) now **only** matches a household channel
  on a `flatrate` entry; rent/buy store entries have `channel=None` so they can't match.
  Verified: Batman Knightfall (rent/buy Sky Store/Apple/Prime) + has sky/apple/prime → **NOT
  free**; Stranger Things (flatrate Netflix) + has netflix → **free**.
- Frontend `CH_BY_ID`: removed `apple tv store`/`itunes`/`sky store`/`amazon video` aliases so the
  card's channel matching agrees with the backend.
- **Card service chips (`subsBadges`)**: now **sorts your services first** (✓ + green ring) and
  raises the cap to 6, so a selected/owned channel is never hidden behind the "+N" — fixing
  "can't see the channel I selected" on the card.
- `APP_VERSION` → **1.13**. (Hard-refresh/clear phone cache + reload `192.168.0.59:8030`.)

## Prior session (fix trailer + "Free only" = no extra charge, incl. your subs)
User reported: (1) trailer/video no longer plays when you open a title; (2) wants a **"Free"**
option. **Definition (user, clarified):** "free" = **you do not pay EXTRA for it** — i.e. it's
**included in a service you already have** (Netflix, Sky, Apple, Prime…) *or* on a genuinely-free
service. It must **EXCLUDE** rent/buy per-title charges (Sky Store, Apple TV Store, Prime
pay-movies). (First attempt wrongly defined free = "genuinely-free services only" — user corrected
it; this session fixes that.)

- **Trailer bug (root cause):** `_tmdb_get` routed detail endpoints to the **`/4` API base,
  which 404s / returns 0 on *everything*** (verified live: the v4 token only works on the
  `/3` base — `/3/tv/{id}/videos` = 7 results, `/4/…` = 0). So the detail's `/videos` fetch
  got nothing → no trailer. **Fix:** `_DETAIL_RE` no longer includes `watch/providers`, and
  `_tmdb_get` now **always stays on `/3`**, just swapping the *signing key* (v4 token →
  v3 key). Never touches `TMDB_BASE_V4`. (`backend/app/main.py` `_tmdb_get`.)
  - Also: `_map_tmdb_detail` + the enrich fallback now use a new `_pick_trailer()` that
    prefers a real **Trailer** over featurettes/clips (TMDB lists featurettes first). Verified
    Stranger Things → the actual Season 1 Trailer, not a featurette.
  - Frontend: the enrich trailer handler now **replaces** the placeholder iframe's `src`
    (it only *appended* before); queries the `iframe` so the label stays intact.
- **Trailer fix:** as before — `_tmdb_get` stays on `/3` (the `/4` base 404s/returns nothing),
  `_pick_trailer()` prefers a real trailer, enrich handler replaces the placeholder iframe src.
- **"Free only" filter = NO EXTRA CHARGE.** Rule (server + client, kept in sync):
  a title counts as free if it has (a) a provider on a **genuinely-free** service
  (`FREE_CHANNELS = {bbc, itvx, channel4, channel5, freevee, pluto, tubi}` — free at all, so it
  always counts) **OR** (b) a **`flatrate`** entry on a channel in the **user's household**.
  A **rent/buy** entry on a service you have does **NOT** count (that's the pay-extra to
  exclude).
  - Backend: `Query.household` + `free_only` route params; the free filter in `search()` is
    **server-side** (mirrors the client) so the returned page is exact, and it works even for a
    service you *have but aren't filtering by*. `need_providers` includes `free_only`.
  - Frontend: `state.freeOnly`; `doSearch()` sends `free_only=true` + the household subs
    (`getHousehold()`); `classify()` gains `noExtra` (same rule as backend); **Free** badge;
    client-side filter; checkbox tooltip + empty-state copy.
  - **Household must be set in Settings → Your subscriptions** for the per-sub rule to fire
    (the genuinely-free part works regardless).
- Verified live (all as expected): Stranger Things + has-Netflix → **free** (flatrate you have);
  Doctor Who + has-Netflix → **free** (BBC genuinely free, even though not owned); Michael(2026)
  rent/buy-only + has Apple/Prime/Sky/Rakuten → **not free** (would pay extra). Earlier bug where
  any household-membership (incl. rent/buy) counted — fixed by requiring `type==='flatrate'`.
- `APP_VERSION` → **1.12**. (Hard-refresh/clear phone cache + reload `192.168.0.59:8030`.)

## Prior session (card/detail provider mismatch → consistent key + merge + honest message)
User reported: a title clearly **on Netflix** (card shows the ✓ Netflix badge) but its
detail modal said **"Not on a UK service we track."** Root cause + fix:

- **Key mismatch** between the two provider passes. The *search* pass gets providers via
  the **v3 key** (the same key that runs the `with_watch_provider` pre-filter, so the card
  badge is v3-derived). But `_providers()` tried the **v4 token FIRST**, so the detail's
  `enrich` refetch hit the v4 token, which returns **empty** for that title → the detail
  unconditionally **overwrote** the card's Netflix with nothing.
  - **Fix 1 — consistency:** `_providers()` now tries **v3 key first**, v4 token as
    fallback. Same key everywhere ⇒ card badge and detail list always agree. (v3's data is
    stale for *some* titles, but v4's is stale for *others* — picking one source ends the
    contradiction. A fully-accurate source would need a JustWatch key, see Blocked below.)
  - **Fix 2 — merge, never replace:** `POST /api/enrich` accepts `platforms` (the card's
    list, now sent from the frontend) and, if its own refetch is *thinner*, merges the card's
    rows in instead of discarding them. Guarantees the detail never loses a service the card
    already showed.
  - **Fix 3 — honest message:** the empty "where to watch" line no longer claims "Not on a UK
    service we track"; it now lists exactly which services are tracked and notes the title may
    be rent/buy-only or on a service we don't list.
- **Selection colour = GREEN** (`.chip.seld`); service chips: "yours" (✓ + green ring) and the
  filtered-by service stay full-brightness; only services you *don't* have AND aren't filtering
  by are faded (78%). (Carried over from the prior contrast work.)
- Verified live: Bridgerton card + enrich both return `Netflix flatrate` + `Netflix Standard
  with Ads` (agree, not empty). All `<script>` blocks pass `node --check`; `main.py`/`channels.py`
  compile; server restarted clean, `/api/meta` 200.
- `APP_VERSION` → **1.10**. (User must hard-refresh/clear phone cache + reload `192.168.0.59:8030`
  to pick up the new frontend.)

## Prior session (fixing "nothing shows" + caching + v4 providers)
User reported: nothing shows, services unseeable, 0 results. Root causes found + fixed:

- **A JS syntax error** (`$('#x')?.onclick=...`) had crashed the whole page script →
  blank/0. Fixed + added a syntax-check habit (extract `<script>` blocks, `node --check`).
- **`kind=any` now = movies + shows** (two discover pools merged) — before it only ran
  the *movie* pool, so series-only titles never appeared. (`search()` route: `dks` list.)
- **Service filters returned 0** because the v3 key's `watch/providers` data is stale
  (Stranger Things → Apple TV; The Crown → nothing). **Discovered the v4 Read token CAN
  fetch `/3/{tv|movie}/{id}/watch/providers` via Bearer** (earlier I wrongly assumed it
  can't). `_providers()` now tries the **v4 token first**, v3 key as fallback. Netflix
  filter now returns 200 (was 0).
- **Caching (the user's ask)**: added `_PROV_CACHE` (per title, 24h TTL, 6h for misses)
  + `_CAST_CACHE` (credits, 7d). Re-selecting a service / re-searching is now **~0.4s**
  (was 14–27s). Initial load ~2.4s cold → ~0.4s warm.
- **Live progress fixed**: `_gather_bounded` progress is now **monotonic** (never
  resets backwards) + a `checking` phase label, so a service search shows
  "Checking where it streams… N/M" ticking instead of a stuck "Searching…".
- **Speed**: channel-filter provider fan-out concurrency 8→100; `where=true` no longer
  fetches providers for a plain browse (`need_providers` gate + new `stream_only` param).
- **Contrast**: service chips now solid fills + always-visible border + bolder; the
  selected service button is bright red (`.chip.seld`) in both themes.
- **Empty-state message** no longer mis-blames "stale keys"; it now explains the actual
  reason (service/mood/`I can stream` filter).
- `APP_VERSION` → **1.9**.

## Prior session (two TMDB keys: v3 providers + v4 details)
The user's TMDB account page shows **two** keys: "API Key" (v3) and "API Read
Access Token" (v4). Discovered they do *different* jobs, so the app now uses **both**:
- **v3 "API Key"** (their existing key, in `.env` `SF_TMDB_API_KEY`) → powers
  `search`, `discover`, and **`watch/providers`** (streaming services). This is the
  ONLY key type that can fetch providers — a v4 token 404s on `/watch/providers`.
- **v4 "API Read Access Token"** (new, `SF_TMDB_V4_TOKEN`) → powers the **detail**
  endpoints (`/tv/{id}`, `/movie/{id}` with `credits,videos,external_ids`) + person
  filmography. A v3 classic key **404s** on these, so without the v4 token, *some*
  titles have no cast/trailer/ratings.

Implementation:
- `config.py`: added `tmdb_v4_token: str = ""` (env `SF_TMDB_V4_TOKEN`).
- `main.py`:
  - `_ctx = {"tmdb_key": None, "v4_key": None, "v4_style": None}` (was `key_style`).
  - `set_tmdb_key(key, v4_key=None)`, `tmdb_v4_key()`, `_detect_v4_style()`
    (Bearer vs param probe), `_attach_auth()`.
  - `_tmdb_get(url, params)` now **routes by URL**: a `_DETAIL_RE` regex matches
    `/tv/{id}` & `/movie/{id}` (incl. `/credits`,`/videos`) → uses the **v4 key** on
    `/4` (or `/3` if the pasted key is v3-style). Everything else (search/discover) →
    v3 key on `/3`. No v4 key + v3 key → falls back to v3 on the detail URL (404s
    gracefully, as before). `TMDB_BASE_V4` moved to the top constants.
  - `_providers()` simplified to one retry loop (always v3 for watch/providers; a v4
    token can't fetch providers). `_parse_gb_watch()` shared parser stays.
  - middleware `inject_key` reads **both** `X-Tmdb-Key` and `X-Tmdb-V4-Key` headers.
  - `POST /api/keys` persists `tmdb_v4_token` (adds `SF_TMDB_V4_TOKEN=` line) +
    validates each changed key; returns `validation={api_key?, v4_token?}`.
  - `_key_status()` now returns `tmdb`, **`tmdb_v4`**, `serper`.
  - `_validate_tmdb()` unchanged (Bearer probe → "v4", param probe → "v3").
- `frontend/index.html`:
  - `tmdbV4Key()/setTmdbV4Key()` + `tmdbHeaders()` (sends both keys as headers); all
    fetch call-sites (fetchMeta, doSearch, enrich, person) use `tmdbHeaders()`.
  - Settings shows **two** TMDB boxes: "where to watch (API Key)" + "details (API Read
    Access Token, v4)". v4 is labelled optional/recommended. Save persists both and
    shows per-key ✓/rejected feedback.
  - `APP_VERSION` → **1.8**.
- Verified (puppeteer): settings shows both fields; multi-genre still 100; TV cards
  show services; detail modal renders; theme toggles; **zero JS errors**.
  ⚠️ Gotcha hit + fixed: a `${badge(x,'a':'b')}` ternary-as-argument broke the whole
  render script ("missing ) after argument list") — always write `${badge(x, x?'a':'b')}`.

## Prior session (multi-genre union + TV streaming)
Fixed the two long-standing complaints: (1) picking 2+ genres/moods → 0 items, and
(2) most cards showing no streaming service.

- **Multi-genre = UNION (OR), not intersection**: TMDB's `with_genres` is AND, so
  picking e.g. Thriller+Action (5 genres) collapsed the pool to ~3 results. `_tmdb_discover`
  now fetches **each selected genre separately** and merges + dedups by TMDB id.
  `per_bucket = max(40, max_titles // n_buckets)` bounds the total requests. One genre
  → single query (no extra cost); no genres → one unrestricted query. Verified: 0→100.
  - Refactor: pulled paging into `_tmdb_discover_pages(path, params, max_titles, origin, pid)`;
    `_tmdb_discover` builds one base param set, then fans out per genre via `asyncio.gather`.
- **TV streaming fixed via v4-capable provider lookup**: the TMDB key in `.env` is a
  **v3 classic key** (32-char). Its v3 `watch/providers` is sparse/broken for TV
  (Arrow/Friends/Doctor Who/Peaky Blinders → empty; only ~5-8 of 12 famous shows had data).
  Movies worked (~10/12). There is **no clean free fallback**: the justwatch v2 API is
  deprecated (404s everywhere, site is JS-rendered/blocked), and v4 requires a key.
  So `_providers(r: Result)` now **branches on the detected key style**:
  - `key_style == "bearer"` (v4 key) → hits `api.themoviedb.org/4/{tv|movie}/{id}/watch/providers`
    (works for **every** title) — same `_parse_gb_watch` parser (v3/v4 share the
    `{provider_id, provider_name}` shape).
  - `key_style == "param"` (v3 classic, the current key) → v3 endpoint (as before).
  - `_parse_gb_watch(data)` is the shared GB-bucket → platform-dict mapper.
  - Even the v3 key now yields **~85/100** TV cards with a service (current popular
    shows are well-listed; the sparse ones are older/obscure titles).
  - `tmdb_id` is present for TV (from discover), so no extra id-resolution needed.
- **Settings: key is now editable + type-aware**:
  - `#k_tmdb` is **no longer readonly** when a key is set. Placeholder: "leave blank to
    keep current, or paste a new key". (Save only sends the key if non-blank, so
    blank = keep — the `set_keys` endpoint keeps the existing key when absent.)
  - Added a tip line: v4 (Read) = every film & series; v3 (Classic) = films + some series.
  - `_validate_tmdb` now returns `key_type` ("v3"/"v4"). Settings save shows a warning
    when a **v3** key is in use: "✓ Saved (v3 key) — films fully, series partially.
    For complete series coverage paste a v4 (Read) key."
- **The real fix for 100% TV coverage**: user should paste a **v4 (Read)** key from
  themoviedb.org → Settings → API into the (now editable) TMDB field, then Save.
  The app auto-detects v4 and switches the provider lookup to the v4 endpoint.
- Verified (puppeteer): multi-genre 0→100 with diverse genres; TV 85/100 cards show
  a service; theme/settings/save/detail/actor all green; zero JS errors.

## Prior session (sort control placement + version)
- **Sort control moved to a prominent, labelled spot**: it was a tiny unlabelled
  dropdown buried in the filter row (easy to miss). Now it sits in the results
  header, right side, directly above the cards — a "SORT BY [Relevance ▾]" label +
  select, opposite the "N results" count. Removed the duplicate from the filter row.
  The `#sort` change handler is now null-safe (`?.`) since the select is absent
  during the loading (spinner) state.
- **App version badge**: `const APP_VERSION = '1.6'` (top of the script). Shown
  faintly top-right in the header next to the theme + settings buttons.
  ➤ **To bump:** edit `APP_VERSION` in `frontend/index.html` (~line 123). Frontend
  is served from disk, so a browser refresh picks it up — no backend restart needed
  for frontend-only changes. (Backend changes DO need a restart: see "Run it".)
- Verified in browser: sort re-sorts (Relevance→Year newest→A–Z), Reset restores
  "relevance", all button tests still green.

## Prior session (card polish + sorting)
- **Sorting**: renamed the sort options to be self-explanatory — "Year (newest)",
  "Year (oldest)", "RT critic %", "RT audience %", "Title A–Z" (plus Relevance,
  Top rated, TMDB score, Most voted). Backend `_sort_key` accepts `newest`/`oldest`/
  `rt_critic`/`rt_audience`/`rating`/`pct`/`tmdb`/`votes`/`title`.
- **Card redesign** (the "cut off / odd bits" fix):
  - Removed the redundant standalone "Film/Series" uppercase tag → now a compact
    "FILM · 2017" inline row above the title.
  - Title: 15px bold, `line-clamp-3`, white with a text-shadow, over a STRONGER
    bottom scrim in `posterBg()` so it's legible on any art. No more mid-word cuts.
  - Genre pills: solid `rgba(0,0,0,.5)` + white + faint border (readable on posters).
  - Score badge (top-left): always `★ NN%`, 3-tier colour via `scoreColor()`
    (green ≥60 / amber 40–59 / red <40), source = RT critic% else TMDB*10.
  - "✓ Stream" flag (top-right): service name on its own line (was "STREAMParamount+"),
    quiet amber "Rent / Buy" tag for non-sub titles. Green border on streamable cards.
  - `subsBadges()` returns a wrapped row of solid brand chips (≤5, then "+N"); "yours"
    gets a ✓ + bright ring, out-of-scope dimmed.
- Verified desktop + mobile screenshots, all button tests green.

## Prior sessions (in place): live progress counter; picker = my services only;
## settings max-results + flexible sort + RT badges.

## THIS SESSION (live progress + service picker scoped to my subs)
- **Live "pulling N…" counter**: backend now tracks search progress in `PROGRESS`
  (dict keyed by `progress_id`), updated as it pulls the pool (`pulled`), attaches
  providers (`providers`, ticks N/total via `_gather_bounded(progress_key=…)`),
  reads RT (`ratings`), then `finishing`. New `GET /api/progress?pid=…` endpoint
  (auto-prunes >5min). Frontend `doSearch` mints a `progress_id`, sends it, and
  polls every 500ms while loading; the loadbar row now shows a live status
  (`#progStatus`) beside the indeterminate bar. Shows "Checking services… 111/588"
  on slow searches; fast/cached ones just flash "Searching…".
- **Service picker now shows ONLY your subscribed services** (household from
  Settings), not all 24. `subscribedChannels()` helper; "All" = all subscribed;
  header relabelled "Your services (the ones you have — pick to filter)".
  Empty household → a "add yours in Settings" prompt chip (opens settings).
  NOTE: you can only filter by services you tick in Settings. To browse a service
  you DON'T subscribe to, you'd have to tick it there first. (User chose "only my subs".)

## Prior session (still in place): settings max-results + flexible sort + RT badges

## Current state: WORKING

- App runs via `./start.sh` (or `start-lan.sh`). Binds 0.0.0.0.
  - Mac: http://127.0.0.1:8030
  - iPhone/LAN: http://192.168.0.59:8030 (re-check with `ipconfig getifaddr en0`)
- Frontend: `frontend/index.html` (single file). Backend: `backend/app/` (FastAPI).
- All button/flow tests green (puppeteer suite in
  `/var/folders/ts/j5qfkr794b7__40tpyn943th0000gn/T/opencode/sftest/` — test.js, test3.js).

## What's solid (verified this session)

### Settings max-results control (NEW)
- Settings-panel-ish inline: a **Max results** dropdown next to sort,
  options 25 / 50 / 100 / 200 / 500 / 1000.
- `state.limit` default is now **100** (was 60). Header shows "N results".
- Persisted to `localStorage['sf_max_results']` via `getLimit()/setLimit()`;
  applied at boot (`state.limit = getLimit()`), reset by Reset (→100).
- Backend caps it: `limit = min(max(1, q.limit), settings.max_results)` where
  `settings.max_results = 1000` (in `config.py`). A client asking for 9999 is capped.

### Flexible sorting (NEW)
- Sort dropdown now: Relevance, Top rated, TMDB score, RT critic, RT audience,
  Most voted, A–Z, Newest, Oldest.
- Backend `_sort()` → `_sort_key()`. Rating sorts put missing-value titles last.
- **RT on browse**: `_attach_rt(r)` (cached in `_RT_CACHE`) now runs on the final
  page. When sorting by `rt_critic`/`rt_audience`, RT is pre-attached to the first
  200 pool titles so the sort is meaningful (else only the last page would have it).
  RT scrape is the slow part — an RT sort takes ~15–25s. Cache makes re-sorts free.
- **RT badges in detail modal**: `ratingBadges()` shows 🍅 critic + 🍿 popcorn + ★ TMDB.
  Cards show the compact top-left % (posterScore). (No card-level RT chips by design.)

### Channel / service filter (fixed earlier)
- `_map_tmdb` normalises `show`→`tv` so provider lookups hit the right endpoint.
- Channel filter keeps same-titled variants; non-filter dedup prefers higher votes.
- `with_watch_provider` on TMDB discover is **silently ignored** by our classic v3 key
  (returns identical results with/without it) — so we do NOT rely on it. We pull a big
  popularity pool and attach providers per-title instead.
- UK origin filter (`with_origin_country=GB`) is applied **only for UK-only services**
  (`channels.UK_ONLY` = bbc/itvx/channel4/channel5). Subscription services (Netflix,
  Disney, Sky, Prime…) are global, so no origin filter or most foreign titles are dropped.

## KNOWN LIMITATION (open)
- **Channel-filter depth**: Netflix+Horror returns ~34, not the 1274 that exist on
  Netflix. Cause: TMDB discover sorts by popularity and we cap the pool at 1000; only a
  fraction of the top-popularity titles are on Netflix, and `with_watch_provider` can't
  filter server-side with our key. It *works* (returns real Netflix horror), just not
  exhaustive. To go deeper we'd need to fetch more discover pages (slower) or a
  provider-aware endpoint. Not blocking — user can search by title for any specific film.

## Key files / lines
- `backend/app/main.py`:
  - `_tmdb_discover_pages()` — pages one discover query; `_tmdb_discover()` — **UNION**
    (fans out per selected genre via `asyncio.gather`, dedups by id, `per_bucket` cap)
  - `TMDB_BASE_V4`, `_parse_gb_watch()`, `_providers(r)` — provider lookup that
    branches on `key_style` (v4 Bearer vs v3 param); `_attach_providers(r)`
  - `PROGRESS` dict + `_progress()` + `GET /api/progress` — live search progress
  - `_gather_bounded(…, progress_key=, progress_phase=, progress_total=)` — running count
  - `search()` route takes `progress_id`, reports pulled/providers/ratings/finishing
  - `_rt_lookup()` / `_attach_rt()` / `_RT_CACHE` — RT scrape + cache
  - `_sort()` / `_sort_key()` / `_rt_val()` — sorting
  - `search()` route (~line 740) — limit cap, channel filter, RT pre-attach for RT sorts
- `backend/app/config.py`: `max_results: int = 1000`, `rt_enabled`, `http_timeout`
- `backend/app/channels.py`: `UK_ONLY`, `PROVIDER_IDS`, `ID_TO_CHANNEL`, `channel_for()`
- `frontend/index.html`:
  - `state` (limit default 100), `getLimit()/setLimit()` (~line 158)
  - `const APP_VERSION = '1.6'` (~line 121); faint version badge in the header (~line 387)
  - **"Sort by" labelled select in the results header** (right side, above the cards,
    `#resultsHeader`; hidden during the spinner) — the sort control. The filter row
    has only the service picker + Reset. maxResults `<select>` in the filter row.
    Handlers for `#sort`/`#maxResults` are null-safe (`?.`).
  - `subscribedChannels()` — picker shows only household services
  - `doSearch()` mints `progress_id` + polls `/api/progress` (500ms) while loading;
    loadbar row shows `#progStatus`. Picker grid uses `subscribedChannels()`.
  - `ratingBadges()` (~line 282, 🍅+🍿+★), `posterScore()`, `card()` (~line 308)
  - boot applies `state.limit = getLimit()` (~line 646)
  - **Settings `#k_tmdb` is editable** (not readonly) — "leave blank to keep current, or
    paste a new key" + a v3/v4 tip. Save shows a v3-key warning if the key is classic.
  - `data-mood` buttons append to `state.moods`; `doSearch()` appends each mood's genres
    as repeated `genres=` params (OR semantics, backend unions them).

## If starting fresh
1. `cd /Users/marksparrow/testai/stream-finder && ./start.sh`
2. If the server died: it's a background uvicorn — relaunch the same command
   (nohup) or just `./start.sh`.
 3. Quick checks:
    - Multi-genre union: `curl -G .../api/search --data kind=show --data limit=50 --data 'genres=Thriller' --data 'genres=Action'` → expect ~50 (was ~3 before the fix).
    - TV streaming: `curl -G .../api/search --data kind=show --data limit=20 --data where=true` → most should have `platforms`.
    - open http://127.0.0.1:8030 → pick 2 moods → results appear; cards show service chips.
    - To enable full TV coverage: Settings ⚙️ → paste a **v4 (Read)** TMDB key → Save
      (the app auto-detects v4 and switches the provider lookup to the v4 endpoint).

## Decisions made (user-confirmed)
- Ratings sources: **TMDB + Rotten Tomatoes** (critic + audience). NO IMDb/OMDb
  (no free official API; OMDb is unofficial/flaky/1000-per-day — user declined).
- Max-results control: settings field, ceiling **1000**.
- Sort options: **all** (relevance, top rated, TMDB, RT critic, RT audience, votes, A–Z, newest, oldest).
