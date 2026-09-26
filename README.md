# Stream Finder — one-stop what-to-watch

Search **every** screen at once. Type a title, or pick a mood (Horror, Thriller,
Sitcom, Sci-Fi, Kids, Doc, Sport…), and you get films + series with:

- 📺 **Where to watch (UK)** — Netflix, Disney+, Apple TV, Sky, BBC, ITVX,
  Channel 4/5, Prime, Max, Paramount+, rent/buy — on every card + in the detail view
- ✅ **"✓ STREAM" / "RENT / BUY" flags** — cards tell you at a glance whether it's
  on a subscription *you actually have* (no surprise charges)
- 🎚️ **"Only what I can stream"** filter + **⚙️ Settings → Your subscriptions**
  (tick what you have, add your own services anytime)
- ⭐ ratings (Rotten Tomatoes Tomatometer + audience, TMDB vote)
- 🎬 **writeup** (synopsis), cast, director, runtime, year
- ▶️ **trailer** (in the detail view)
- filters: mood, genre, year range, minimum rating, sort

> **Where to watch needs the TMDB key** (it's TMDB's free Watch Providers data,
> region = UK). Shows the *subscriptions* first (e.g. "Dune → Max, Sky
> Cinema"), then rent/buy in the detail view.

### "Do I have it?" (household subscriptions)
Open **⚙️ Settings → Your subscriptions**. Tick the services you have (the common
UK ones come pre-ticked), and add your own. The app then:
- flags each card **✓ STREAM** (green, you have it included) or **RENT / BUY** (orange)
- lets you filter to **only things you can stream**
- clicking a card's **+N** shows the full list of where it's available

Your subscriptions are saved in your browser on this device only — each phone has its
own, so family members can tick different services without affecting each other.

No paid services. It runs locally on your Mac — and, if you want, **from anywhere
in the world** (see [Use it from anywhere](#use-it-from-anywhere-public-no-setup-for-the-viewer)).

## Run it

You don't need to — **launchd starts everything at boot** (both servers, auto-restart
on crash). To check status and get the URLs:

```bash
./status.sh
```

That's it — TV shows, ratings and trailers work with **zero config** (TVMaze +
Rotten Tomatoes, both free).

| Route | Address | For |
|---|---|---|
| On this Mac | `https://127.0.0.1:8443` | you, locally |
| Family (home Wi‑Fi) | `https://192.168.0.59:8443` | anyone on the house Wi‑Fi |
| Anywhere (public) | `https://marks-macbook-pro.tail0003aa.ts.net:10000` | any phone, anywhere — no Tailscale app, no setup |

The old `./start.sh` (HTTP :8030) and `./start-https.sh` still work for manual runs,
but `status.sh` + launchd is the normal way now.

## Unlock movies (one-time, free)

**Click the ⚙️ Settings button** (top-right) → paste your **TMDB key** → **Save**.
Done — no code, no files. It's stored on this Mac only (a small `.env` next to
the app + your browser). Movies (search + discover + trailers) light up
immediately, no restart.

Get a key: https://www.themoviedb.org/settings/api (sign up → **Create an API
key**). *(The amber bar at the top is the same shortcut.)*

> You can also drop the key into `backend/.env` as `SF_TMDB_API_KEY=…` and
> restart — but the Settings button is the easy way.

## Use it from anywhere (public, no setup for the viewer)

The app is published to the **public internet** via **Tailscale Funnel**, so a phone
can open it from anywhere **without the Tailscale app** and without any account or
passcode — same trick as the Assessment Portal's tunnel (that tunnel lives in
`~/AssessmentPortal` and is a separate app; leave its `mac/tunnel-*.sh` files alone).

Share **`https://marks-macbook-pro.tail0003aa.ts.net:10000`** — it just works.

**One-time setup (already done):** Tailscale Funnel public port **10000** → the local
`:4443` server (`tailscale funnel --bg --https=10000 https+insecure://127.0.0.1:4443`).
Port 443 is left free for the Assessment Portal's tunnel (Funnel only allows 443/8443/10000); everything runs via launchd. If the public URL ever stops
working, check on the Mac: `./status.sh` (servers) and that the Tailscale app is
connected.

**Honest trade-off:** the address is internet-reachable. It's a read-only what-to-watch
app (no accounts, no personal data), but anyone with the URL can browse it and can use
the Settings page to *replace* the TMDB key with a bad one (it resets on the next
server restart, and the real keys live in `backend/.env`). Keep the URL for people you
trust. The Mac must be on (screen may sleep) for it to answer.

## Cloud backend (works even when the Mac is off)

A copy of the backend runs on **Render** (free tier) so the app works from anywhere
*without* the Mac. The frontend (GitHub Pages: `https://mas-ghub.github.io/stream-finder/`)
tries the Render backend first, then the family Mac (home Wi‑Fi), then the Tailscale
address — the first that answers wins, and it's remembered on the device.

- Repo: `mas-ghub/stream-finder` (this repo, `backend/` subdir) → Render web service
  `stream-finder-api` → auto-deploys on every push (blueprint: `deploy/render/render.yaml`).
- Render's free tier **sleeps after 15 minutes** of no traffic and takes ~1 minute to
  wake — that's the only rough edge. (Hugging Face Spaces was the original plan, but
  they now require a paid PRO plan to create a Docker Space.)
- The TMDB keys live in Render's Environment settings (never in this repo — `.env` is
  gitignored).

## Optional: better TV ratings

`SF_SERPER_API_KEY` (free tier at https://serper.dev) makes Rotten Tomatoes
ratings resolve for **TV shows** (search-engine lookup). Movies already work
without it.

## What it scans
| Source | What | Config |
|---|---|---|
| TMDB | films + series, genres, cast, trailers | free key (set in `backend/.env`) |
| TVMaze | series, genres, ratings | none |
| Rotten Tomatoes | Tomatometer, audience, cast, trailer | none (scraped) |

## Layout
```
stream-finder/
  status.sh         # URLs + UP/DOWN health (normal day-to-day check)
  start.sh          # manual run (launchd does this automatically now)
  sf-start.sh       # the supervisor launchd runs (4443 / 8443)
  test.sh           # quick API smoke test
  deploy/render/    # Render blueprint (deploy.yaml) for the cloud backend
  backend/
    app/main.py     # FastAPI: /api/search /api/enrich /api/meta + serves UI
    app/config.py   # settings (.env, SF_ prefix)
    app/genres.py   # mood + canonical genre map
    app/models.py   # shared data model
    .env            # your keys (create from the sample below)
  frontend/
    index.html      # the UI (served by the backend at /)
```

> **Honest note on "scan Netflix/Sky/BBC/ITV/Channel 4 directly":** those
> services lock their *current* catalogs behind login + heavy bot-walls, and
> their private APIs change often, so live per-service membership isn't wired
> in yet. This app gives you the **what-to-watch** layer — every title, mood,
> rating and trailer — reliably and free. The backend is built so a
> per-service adapter (e.g. "is this on Netflix UK?") can be dropped in later.
