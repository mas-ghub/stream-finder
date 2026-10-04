"""FastAPI app — one-stop search/discover across streaming services.

Source of truth = TMDB (movies+shows: genres, cast, ratings, trailers).
TVMaze backs the shows side (works with zero config). Rotten Tomatoes enriches
with the Tomatometer, audience score, cast and trailer. Everything degrades
gracefully — with no TMDB key you still get shows via TVMaze.
"""
from __future__ import annotations

import asyncio
import functools
import inspect
import json
import logging
import os
import re
import time

import httpx
from fastapi import FastAPI, Header, Query as FQuery, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from .channels import UK_ONLY, all_channels, channel_for, channel_family, channel_meta, provider_ids_for, PROVIDER_IDS
from .config import settings
from .genres import MOODS, genres_for_mood, normalize_genre, normalize_genres
from .models import Ratings, Result

log = logging.getLogger("streamfinder")

# Optional mount path, e.g. SF_PREFIX=/stream-finder. This lets the app coexist
# with other apps on the same host (e.g. the Tailscale ts.net address shared with
# the business app): Stream Finder then lives at /stream-finder and can't collide.
# Defaults to "" (serve at /) so the local/standalone runs are unchanged.
SF_PREFIX = os.environ.get("SF_PREFIX", "").strip().rstrip("/")

app = FastAPI(title="Stream Finder", version="1.0", root_path=SF_PREFIX)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.middleware("http")
async def inject_key(request, call_next):
    # The backend is SELF-SUFFICIENT: it always uses the keys in backend/.env.
    # A frontend-supplied key (X-Tmdb-*) is honoured ONLY if non-empty and it
    # differs from .env — this lets the Mac-local site keep working if a user
    # pasted a key in-app before .env existed, but the public Pages site (which
    # sends no keys) simply falls through to .env. Empty headers are ignored so a
    # stale/blank browser store can never *clear* the .env keys.
    k = (request.headers.get("x-tmdb-key") or "").strip()
    v4 = (request.headers.get("x-tmdb-v4-key") or "").strip()
    if k or v4:
        set_tmdb_key(k or None, v4 or None)  # None preserves the .env value
    return await call_next(request)

CLIENT = httpx.AsyncClient(
    timeout=settings.http_timeout,
    headers={
        "User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                       "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"),
        "Accept-Language": "en-GB,en;q=0.9",
    },
)

TMDB_BASE = "https://api.themoviedb.org/3"
TMDB_BASE_V4 = "https://api.themoviedb.org/4"
TMDB_IMG = "https://image.tmdb.org/t/p/original"
TVMaze_BASE = "https://api.tvmaze.com"

_cache: dict[tuple, tuple[float, object]] = {}
# watch/providers results, keyed by (kind, tmdb_id). Cached so re-selecting the same
# service/title doesn't re-hit TMDB (which is what made the Netflix filter take ~14s).
# A 404 (title with no GB data) is cached for a shorter window so it can appear later.
_PROV_CACHE: dict[tuple, tuple[float, list]] = {}
_PROV_TTL = 24 * 3600  # availability is stable for days
_PROV_MISS_TTL = 6 * 3600
# credits (cast/directors) per title — stable, cached so re-searches are instant.
_CAST_CACHE: dict[tuple, tuple[float, dict]] = {}
_CAST_TTL = 7 * 24 * 3600
# Rotten Tomatoes scrape results (tomatometer/audience/cast/trailer). Defined HERE (not
# down by _rt_lookup) because _persist_load() below restores it at import time.
_RT_CACHE: dict[tuple, dict] = {}
_RT_MISS_TTL = 60  # seconds to remember a failed lookup before retrying


def _cached(key: tuple, ttl: int, make: callable):
    now = time.time()
    hit = _cache.get(key)
    if hit and now - hit[0] < ttl:
        return hit[1]
    val = make()
    _cache[key] = (now, val)
    return val


def _prov_cached(key: tuple) -> list | None:
    hit = _PROV_CACHE.get(key)
    if hit and time.time() - hit[0] < (hit[1] and _PROV_MISS_TTL or _PROV_TTL):
        return list(hit[1]) if hit[1] is not None else []
    return None


# ---------------------------------------------------------------------------
# Persistent disk cache
#
# The in-memory caches above are wiped every time the (free-tier Render) container
# is recycled after sleeping, so the first search after an idle period re-fetches
# everything (providers + RT scrape + cast) and feels very slow. We mirror the
# long-lived caches (providers, cast, RT) to a JSON file on disk and restore them
# on boot, so a cold container starts with a WARM cache. Render gives each service
# a local persistent disk; we write there, falling back to a temp dir if it's not
# writable. Keys are tuples (not JSON-serialisable) so we join them with a unit
# separator when persisting.
# ---------------------------------------------------------------------------
_PERSIST_PATH = os.environ.get(
    "SF_CACHE_FILE",
    os.path.join(os.environ.get("SF_CACHE_DIR", "/tmp"), "sf_cache.json"),
)
_PERSIST_TTL = {  # how long a restored entry stays valid (seconds)
    "prov": _PROV_TTL,
    "cast": _CAST_TTL,
    "rt": settings.cache_ttl,
}
_persist_loaded = False


def _key_str(key: tuple) -> str:
    return "\x1f".join(str(p) for p in key)


def _key_tup(s: str) -> tuple:
    # Reconstruct the original components. JSON round-trips ints as strings, so turn
    # anything that is purely digits back into an int — the prov/cast/rt caches all
    # key on tmdb_id (an int), so without this a restored entry would never match.
    out = []
    for p in s.split("\x1f"):
        out.append(int(p) if p.isdigit() else p)
    return tuple(out)


def _persist_save() -> None:
    """Snapshot the long-lived caches to disk (best-effort, never fatal)."""
    try:
        now = time.time()
        # list(...) snapshots are atomic under the GIL, so this is safe to run in a
        # worker thread while the event loop keeps mutating the caches.
        blob = {
            "prov": {_key_str(k): [ts, v] for k, (ts, v) in list(_PROV_CACHE.items())
                     if v is not None and now - ts < _PROV_TTL},
            "cast": {_key_str(k): [ts, v] for k, (ts, v) in list(_CAST_CACHE.items())
                     if now - ts < _CAST_TTL},
            "rt": {_key_str(k): e for k, e in list(_RT_CACHE.items())
                   if e.get("_t", 0) > now and e.get("_t", 0) - now < _PERSIST_TTL["rt"]},
        }
        with open(_PERSIST_PATH, "w") as f:
            json.dump(blob, f)
    except Exception as e:  # noqa: BLE001
        log.debug("cache persist failed: %s", e)


def _persist_load() -> None:
    """Restore the long-lived caches from disk on boot (best-effort)."""
    global _persist_loaded
    _persist_loaded = True
    if not os.path.exists(_PERSIST_PATH):
        return
    try:
        with open(_PERSIST_PATH) as f:
            blob = json.load(f)
    except Exception as e:  # noqa: BLE001
        log.debug("cache load failed: %s", e)
        return
    now = time.time()
    for ks, v in (blob.get("prov") or {}).items():
        if isinstance(v, list) and len(v) == 2 and now - v[0] < _PROV_TTL:
            _PROV_CACHE[_key_tup(ks)] = (v[0], v[1])
    for ks, v in (blob.get("cast") or {}).items():
        if isinstance(v, list) and len(v) == 2 and now - v[0] < _CAST_TTL:
            _CAST_CACHE[_key_tup(ks)] = (v[0], v[1])
    for ks, e in (blob.get("rt") or {}).items():
        if isinstance(e, dict) and e.get("_t", 0) > now:
            _RT_CACHE[_key_tup(ks)] = e
    n = len(_PROV_CACHE) + len(_CAST_CACHE) + len(_RT_CACHE)
    if n:
        log.info("restored %d cached entries from %s", n, _PERSIST_PATH)


# Load the persisted cache as early as possible so the first search after a cold
# start hits it. Done at import time (module load) which is before any request.
_persist_load()


_save_pending = False


def _kick_save() -> None:
    """Run the disk snapshot in a worker thread so it can never block the event loop."""
    global _save_pending
    _save_pending = False
    try:
        asyncio.get_event_loop().run_in_executor(None, _persist_save)
    except RuntimeError:
        pass


def _mark_cache_dirty() -> None:
    """Call after mutating a persisted cache. One save is queued at a time (every call used
    to queue its own full-cache JSON dump on the event loop — dozens per page on the 0.1-CPU
    free instance, which stalled the health check and got the process killed)."""
    global _save_pending
    if _save_pending:
        return
    try:
        t = asyncio.get_event_loop()
    except RuntimeError:
        return
    _save_pending = True
    t.call_later(60.0, _kick_save)


# Per-request TMDB key override (the frontend can pass a key stored in
# localStorage so the user never has to edit .env).
# v4 (Read Access Token) auth style: "bearer" | "param" | None (undecided).
_ctx = {"tmdb_key": None, "v4_key": None, "v4_style": None}

# Live search progress, polled by the frontend while a search is in flight.
# Keyed so a second concurrent search doesn't clobber the first.
PROGRESS: dict[str, dict] = {}


def _progress(key: str, **fields) -> None:
    p = PROGRESS.setdefault(key, {})
    p.update(fields)
    p["_t"] = time.time()
    # partial_ready: the frontend re-serialises this snapshot to its clients on every
    # change, so keep the size honest (drop superseded snapshots).
    pr = p.get("partial_results")
    if isinstance(pr, list) and len(pr) > 80:
        p["partial_results"] = pr[-80:]


def set_tmdb_key(key: str | None, v4_key: str | None = None) -> None:
    k = (key or "").strip() or None
    v4 = (v4_key or "").strip() or None
    if _ctx["tmdb_key"] != k or _ctx["v4_key"] != v4:
        _ctx["v4_style"] = None  # re-detect if either key changed
    _ctx["tmdb_key"] = k
    _ctx["v4_key"] = v4


def tmdb_key() -> str:
    """v3 key: used for search + discover + watch/providers."""
    return _ctx["tmdb_key"] or settings.tmdb_api_key


def tmdb_v4_key() -> str:
    """v4 Read Access Token: used for the detail/credits/videos endpoints
    (v3 classic keys 404 on those). Empty if the user hasn't supplied one."""
    return _ctx["v4_key"] or settings.tmdb_v4_token


def _tmdb_auth():
    return tmdb_key()


async def _detect_v4_style() -> None:
    """Decide once whether the v4 key needs a Bearer header (v4 tokens) or the
    classic `api_key` param (a v3 key pasted into the v4 field)."""
    if _ctx["v4_style"] is not None or not tmdb_v4_key():
        return
    k = tmdb_v4_key()
    probe = await CLIENT.get(f"{TMDB_BASE}/configuration", headers={"Authorization": f"Bearer {k}"})
    _ctx["v4_style"] = "bearer" if probe.status_code == 200 else "param"


def _attach_auth(k: str, style: str, url: str, params: dict | None):
    if style == "bearer":
        return CLIENT.get(url, params=params, headers={"Authorization": f"Bearer {k}"})
    p = dict(params or {})
    p["api_key"] = k
    return CLIENT.get(url, params=p)


# Detail endpoints (/{tv|movie}/{id}/…) with optional credits|videos subpaths.
# Both the v3 "API Key" and the v4 "Read Access Token" serve these on the /3 base.
# (The /4 base 404s on *all* of them — verified — so we never switch base; we only
# switch *which* key signs the request.)
_DETAIL_RE = re.compile(r"(?:/tv|/movie)/\d+(?:/|$|\?|/(credits|videos))")


async def _tmdb_get(url: str, params: dict | None = None) -> httpx.Response:
    """GET a TMDB endpoint. detail/credits/videos sign with the v4 Read token when
    one is set (it's the more permissive key); search + discover sign with the v3
    key. Everything stays on the /3 base — the /4 base returns no data."""
    if _DETAIL_RE.search(url):
        v4 = tmdb_v4_key()
        if v4:
            if _ctx["v4_style"] is None:
                await _detect_v4_style()
            return await _attach_auth(v4, _ctx["v4_style"] or "param", url, params)
        if tmdb_key():
            return await _attach_auth(tmdb_key(), "param", url, params)
        return await CLIENT.get(url, params=params)
    # search + discover
    k = tmdb_key()
    if not k:
        return await CLIENT.get(url, params=params)
    return await _attach_auth(k, "param", url, params)


# ---------------------------------------------------------------------------
# Schemas
# ---------------------------------------------------------------------------
class Query(BaseModel):
    q: str | None = None
    kind: str = "any"
    genres: list[str] = Field(default_factory=list)
    mood: str | None = None
    year_min: int | None = None
    year_max: int | None = None
    min_rating: float | None = None
    min_rt: int | None = None
    channels: list[str] = Field(default_factory=list)
    where: bool = False
    stream_only: bool = False
    free_only: bool = False
    english_only: bool = False
    rt_off: bool = False  # client opted out of Rotten Tomatoes (skips the slow scrape)
    rt_on: bool = False   # client explicitly ENABLED Rotten Tomatoes in its settings —
                          # RT is opt-in: without this flag the scrape never runs
    household: list[str] = Field(default_factory=list)
    fetch_all: bool = False
    pool: int | None = None
    sort: str = "relevance"
    limit: int = 60
    offset: int = 0  # "load more" pagination: start of the next batch


class SourceStatus(BaseModel):
    key: str
    name: str
    enabled: bool
    configured: bool
    message: str = ""


# ---------------------------------------------------------------------------
# Genre id maps (TMDB)
# ---------------------------------------------------------------------------
_MOVIE_GENRE_IDS = {
    "Action": 28, "Adventure": 12, "Animation": 16, "Comedy": 35, "Crime": 80,
    "Documentary": 99, "Drama": 18, "Fantasy": 14, "Horror": 27, "Mystery": 9648,
    "Romance": 10749, "Sci-Fi": 878, "Thriller": 53, "Kids & Family": 10751,
    "Western": 37, "History": 36, "War": 10752, "Music": 10402, "Biography": 36, "Sports": 10753,
}
_TV_GENRE_IDS = {
    "Action": 10759, "Adventure": 10759, "Animation": 16, "Comedy": 35, "Crime": 80,
    "Documentary": 99, "Drama": 18, "Fantasy": 14, "Horror": 27, "Mystery": 9648,
    "Romance": 10749, "Sci-Fi": 10765, "Thriller": 53, "Kids & Family": 10762,
    "Western": 37, "Reality": 10764, "Music": 10763, "Sports": 10753, "News": 10755, "Stand-up": 35,
}
_GENRE_IDS = {"movie": _MOVIE_GENRE_IDS, "show": _TV_GENRE_IDS, "any": _MOVIE_GENRE_IDS}

# TMDB has no real "Sports" genre — id 10753 returns 0 for both films and series, so
# the Sport mood used to come back empty (only TVMaze tags "Sports", and TVMaze has no
# where-to-watch data, so it vanished under a service filter). Sports titles are tagged
# with TMDB's "sport" KEYWORD (6075) instead, so we browse that and stamp the results as
# the Sports genre, which the normal genre filter then keeps.
_SPORTS_GENRE_ID = 10753
_SPORTS_KEYWORD = 6075

# Inverse: TMDB genre id -> canonical name (for mapping search results, which
# return genre_ids rather than genre objects).
_MOVIE_NAME_BY_ID: dict[int, str] = {}
for _k, _v in _MOVIE_GENRE_IDS.items():
    _MOVIE_NAME_BY_ID.setdefault(_v, _k)
_TV_NAME_BY_ID: dict[int, str] = {}
for _k, _v in _TV_GENRE_IDS.items():
    _TV_NAME_BY_ID.setdefault(_v, _k)


def _genres_from_item(item: dict, kind: str) -> list[str]:
    """TMDB search results give genre_ids; detail gives genre objects. Handle both."""
    if item.get("genres"):
        return normalize_genres([g.get("name", "") for g in item["genres"]])
    by_id = _MOVIE_NAME_BY_ID if kind == "movie" else _TV_NAME_BY_ID
    return normalize_genres([by_id.get(i) for i in item.get("genre_ids", [])])


def _runtime(rt) -> int | None:
    if not rt:
        return None
    try:
        return int(float(rt))
    except (TypeError, ValueError):
        return None


def _external(tmdb_id: int | None, kind: str) -> str | None:
    return f"https://www.themoviedb.org/{kind}/{tmdb_id}" if tmdb_id else None


# ---------------------------------------------------------------------------
# Mapping
# ---------------------------------------------------------------------------
def _map_tmdb(item: dict, kind: str | None = None) -> Result:
    # Discover results carry no media_type; fall back to the requested kind
    # (normalise "show"->"tv" so the provider/credits lookups hit the right endpoint).
    media = item.get("media_type") or (kind or ("movie" if "release_date" in item else "show"))
    media = "tv" if media in ("show", "tv") else media
    k = "show" if media == "tv" else "movie"
    date = item.get("release_date") or item.get("first_air_date") or ""
    vote = item.get("vote_average")
    return Result(
        title=item.get("title") or item.get("name") or "Untitled",
        kind=k,
        year=int(date[:4]) if date else None,
        genres=_genres_from_item(item, k),
        poster_url=(TMDB_IMG + item["poster_path"]) if item.get("poster_path") else None,
        backdrop_url=(TMDB_IMG + item["backdrop_path"]) if item.get("backdrop_path") else None,
        overview=item.get("overview") or None,
        ratings=Ratings(tmdb_vote=round(vote, 1) if vote else None, tmdb_count=item.get("vote_count")),
        runtime_minutes=_runtime(item.get("runtime")),
        tmdb_id=item.get("id"),
        language=item.get("original_language"),
        external_url=_external(item.get("id"), k),
        sources=["tmdb"],
    )


def _strip_html(html: str | None) -> str:
    return re.sub(r"<[^>]+>", "", html or "").strip()


def _map_tvmaze(show: dict) -> Result:
    ext = show.get("externals", {}) or {}
    img = show.get("image", {}) or {}
    avg = ((show.get("rating") or {}).get("average") or 0)
    imdb = ext.get("imdb")
    return Result(
        title=show.get("name", "Untitled"),
        kind="show",
        imdb_id=imdb,
        year=int(show["premiered"][:4]) if show.get("premiered") else None,
        genres=normalize_genres(show.get("genres", [])),
        poster_url=img.get("original") or img.get("medium"),
        overview=_strip_html(show.get("summary"))[:400] or None,
        ratings=Ratings(tmdb_vote=round(avg, 1) if avg else None),
        runtime_minutes=show.get("averageRuntime") or show.get("runtime"),
        external_url=(f"https://www.imdb.com/title/{imdb}/") if imdb else None,
        sources=["tvmaze"],
    )


def _pick_trailer(results: list) -> dict | None:
    """Pick the best trailer from a TMDB videos list. Prefer a real trailer
    (type Trailer, or a name that says so) over featurettes/clips; fall back to
    the first YouTube clip only if no trailer exists."""
    vids = [v for v in (results or []) if v.get("site") == "YouTube" and v.get("key")]
    if not vids:
        return None
    trailers = [v for v in vids if v.get("type") == "Trailer" or "trailer" in (v.get("name") or "").lower()]
    return (trailers or vids)[0]


def _map_tmdb_detail(d: dict, kind: str) -> Result:
    if not d:
        return Result(title="?", kind=kind)
    date = d.get("release_date") or d.get("first_air_date") or ""
    vote = d.get("vote_average")
    crew = [c.get("name") for c in d.get("credits", {}).get("crew", []) if c.get("job") == "Director"][:5]
    cast = [c.get("name") for c in d.get("credits", {}).get("cast", []) if c.get("name")][:12]
    cast_full = [
        {"name": c.get("name"), "person_id": c.get("id"),
         "profile": (TMDB_IMG + c["profile_path"]) if c.get("profile_path") else None}
        for c in d.get("credits", {}).get("cast", []) if c.get("name")
    ][:12]
    vid = _pick_trailer(d.get("videos", {}).get("results", []))
    imdb = (d.get("external_ids") or {}).get("imdb_id")
    return Result(
        title=d.get("title") or d.get("name") or "Untitled",
        kind=kind,
        year=int(date[:4]) if date else None,
        genres=_genres_from_item(d, kind),
        poster_url=(TMDB_IMG + d["poster_path"]) if d.get("poster_path") else None,
        backdrop_url=(TMDB_IMG + d["backdrop_path"]) if d.get("backdrop_path") else None,
        overview=d.get("overview") or None,
        directors=crew,
        cast=cast,
        cast_full=cast_full,
        ratings=Ratings(tmdb_vote=round(vote, 1) if vote else None, tmdb_count=d.get("vote_count")),
        runtime_minutes=_runtime(d.get("runtime")),
        tmdb_id=d.get("id"),
        language=d.get("original_language"),
        imdb_id=imdb,
        trailer_url=(f"https://www.youtube.com/embed/{vid['key']}") if vid else None,
        external_url=(f"https://www.imdb.com/title/{imdb}/") if imdb else _external(d.get("id"), kind),
        sources=["tmdb"],
    )


# ---------------------------------------------------------------------------
# YouTube trailer helper (the Rotten Tomatoes scraper that lived here was removed)
# ---------------------------------------------------------------------------
def _youtube_id(url: str) -> str | None:
    m = re.search(r"(?:watch\?v=|youtu\.be/|embed/)([\w-]{6,})", url)
    return m.group(1) if m else None


async def _rt_lookup(title: str, year: int | None, kind: str = "movie") -> dict:
    """Rotten Tomatoes scraping was removed (against their terms). Kept as a no-op so the
    old call sites (all gated on settings.rt_enabled, now always False) stay harmless."""
    return {}


# ---------------------------------------------------------------------------
# TMDB fetch
# ---------------------------------------------------------------------------
async def _tmdb_search(q: str, kind: str, english_only: bool = False) -> list[dict]:
    path = {"movie": "search/movie", "show": "search/tv", "any": "search/multi"}[kind]
    extra = {"with_origin_language": "en"} if english_only else {}
    out: list[dict] = []
    for page in (1, 2):
        r = await _tmdb_get(f"{TMDB_BASE}/{path}", {"query": q, "include_adult": "false", **extra, "page": page})
        if r.status_code == 401:
            return []
        r.raise_for_status()
        out.extend(r.json().get("results", []))
    return out[:100]


async def _tmdb_discover_pages(path: str, params: dict[str, str], max_titles: int, origin_country: str | None = None, progress_id: str | None = None, english_only: bool = False) -> list[dict]:
    """Page through one discover query (single genre, or no genre) up to max_titles.
    A 401 (bad key) short-circuits the whole search, so it aborts early."""
    p = dict(params)
    if origin_country:
        p["with_origin_country"] = origin_country
        p["sort_by"] = "popularity.desc"
        p["vote_count.gte"] = "10"
    if english_only:
        # TMDB's with_origin_language filters on ORIGINAL language, so "en" = made
        # in English (this is the "English only" the user asked for, not dubbed).
        p["with_origin_language"] = "en"
    out: list[dict] = []
    pages = min((max_titles + 19) // 20, 30)  # 20 per page, cap ~600
    # Page 1 alone first: it tells us total_pages (so we never fetch beyond the
    # result set) and doubles as the 401 short-circuit (key check).
    r = await _tmdb_get(f"{TMDB_BASE}/{path}", {**p, "page": 1})
    if r.status_code == 401:
        return []
    r.raise_for_status()
    data = r.json()
    batch = data.get("results", [])
    out.extend(batch)
    if progress_id:
        _progress(progress_id, phase="pulled", done=len(out), total=max_titles)
    if len(out) >= max_titles or not batch:
        return out[:max_titles]
    # Remaining pages IN PARALLEL (bounded burst), not one at a time. Sequential
    # paging was the slowest part of a browse: each bucket waited a whole TMDB
    # round-trip per page (up to 13 rounds), so a 3-genre mood stacked ~13 × TMDB
    # latency before the provider check even started. Browsing 6 at a time keeps
    # the concurrent burst modest while a bucket finishes in ~2 rounds.
    total_pages = min(data.get("total_pages", 1), pages)
    sem = asyncio.Semaphore(6)

    async def grab(pg: int) -> list[dict]:
        async with sem:
            rr = await _tmdb_get(f"{TMDB_BASE}/{path}", {**p, "page": pg})
            if rr.status_code == 401:
                return []
            if rr.status_code in (429, 500, 502, 503):
                await asyncio.sleep(0.6)  # brief backoff, one retry
                rr = await _tmdb_get(f"{TMDB_BASE}/{path}", {**p, "page": pg})
            rr.raise_for_status()
            return rr.json().get("results", [])

    chunks = await asyncio.gather(*(grab(pg) for pg in range(2, total_pages + 1)),
                                  return_exceptions=True)
    for c in chunks:
        if isinstance(c, Exception):
            log.debug("discover page raised %s", c)
            continue
        out.extend(c)
    if progress_id:
        _progress(progress_id, phase="pulled", done=min(len(out), max_titles), total=max_titles)
    return out[:max_titles]


async def _tmdb_discover(kind: str, genres: list[str], year_min: int | None = None, year_max: int | None = None, max_titles: int = 120, origin_country: str | None = None, channels: list[str] | None = None, progress_id: str | None = None, english_only: bool = False, prov_ids: list[int] | None = None) -> list[dict]:
    gids = sorted({_GENRE_IDS[kind].get(normalize_genre(g) or g) for g in genres} - {None})
    path = "discover/movie" if kind == "movie" else "discover/tv"
    dkey = "primary_release_date" if kind == "movie" else "first_air_date"
    base: dict[str, str] = {"sort_by": "vote_average.desc", "vote_count.gte": "150", f"{dkey}.gte": "1950-01-01"}
    if prov_ids:
        # Let TMDB filter by service server-side (free, exact): the pool COMES BACK
        # as "titles on these services", so no per-title provider scan is needed to
        # build the result set — only the displayed cards need their chips attached.
        # NOTE the plural: TMDB's param is with_watch_providerS (the singular is
        # silently ignored — discovered the hard way when "prefiltered" Horror
        # turned out to be full of non-Netflix titles).
        base["with_watch_providers"] = "|".join(str(int(i)) for i in prov_ids)
        base["watch_region"] = "GB"
        # Popular first: a service browse is "what's on Netflix in this mood", and
        # vote_average ordering led with obscure high-rated international titles.
        # The client can still re-sort locally (Top rated / RT / newest…). The vote
        # floor also relaxes here: provider-filtered pools are small, and the global
        # 150-vote floor would cut a 64-title pool to 27.
        base["sort_by"] = "popularity.desc"
        base["vote_count.gte"] = "30"
    if year_min:
        base[f"{dkey}.gte"] = f"{int(year_min)}-01-01"
    if year_max:
        base[f"{dkey}.lte"] = f"{int(year_max)}-12-31"
    if english_only:
        base["with_origin_language"] = "en"
    # "Sports" isn't a TMDB genre (see _SPORTS_GENRE_ID) — browse the sport keyword and
    # stamp the hits as Sports below so the normal genre filter keeps them.
    sports = _SPORTS_GENRE_ID in gids
    if sports:
        gids = [g for g in gids if g != _SPORTS_GENRE_ID]
        base["with_keywords"] = str(_SPORTS_KEYWORD)
    # TMDB's `with_genres` is AND (a title must carry every listed genre), so picking
    # several genres/moods collapses the pool to near-empty. We want OR (union): pull
    # each selected genre separately, then merge + dedup. One genre -> a single query
    # (no extra requests). No genres -> one unrestricted query.
    buckets = [dict(base, with_genres=str(g)) for g in gids] or [dict(base)]
    if progress_id:
        _progress(progress_id, phase="pulled", done=0, total=max_titles)
    per_bucket = max(40, max_titles // len(buckets))  # keep total requests bounded
    chunks = await asyncio.gather(*[
        _tmdb_discover_pages(path, params, per_bucket, origin_country, progress_id, english_only)
        for params in buckets
    ], return_exceptions=True)
    seen: dict[int, dict] = {}
    for chunk in chunks:
        if isinstance(chunk, Exception):
            log.debug("discover bucket raised %s", chunk)
            continue
        for it in chunk:
            if sports:
                ids = list(it.get("genre_ids") or [])
                if _SPORTS_GENRE_ID not in ids:
                    it["genre_ids"] = ids + [_SPORTS_GENRE_ID]
            seen.setdefault(it.get("id"), it)
    out = list(seen.values())
    if progress_id:
        _progress(progress_id, phase="pulled", done=len(out), total=max_titles)
    return out[:max_titles]


async def _tvmaze_search(q: str, match: str | None = None) -> list[dict]:
    """TVMaze's `name` param is fuzzy/ordered, so fetch a batch then keep only
    titles that actually contain the query (case-insensitive)."""
    r = await CLIENT.get(f"{TVMaze_BASE}/shows", params={"name": q, "size": 250})
    r.raise_for_status()
    data = r.json()
    needle = (match or q).lower().strip()
    if not needle:
        return data[:60]
    exact, partial = [], []
    for s in data:
        t = (s.get("name") or "").lower()
        if needle in t:
            (exact if t == needle else partial).append(s)
    out = exact + partial
    out.sort(key=lambda s: 0 if (s.get("name") or "").lower() == needle else 1)
    return out[:60]


async def _tvmaze_discover() -> list[dict]:
    out: list[dict] = []
    for page in range(4):
        r = await CLIENT.get(f"{TVMaze_BASE}/shows", params={"page": page, "size": 25})
        r.raise_for_status()
        out.extend(r.json())
    return out


# ---------------------------------------------------------------------------
# Where-to-watch (TMDB watch providers — free, needs the TMDB key)
# ---------------------------------------------------------------------------
PROVIDER_NAMES = {
    "netflix": "Netflix", "disneyplus": "Disney+", "disney": "Disney+",
    "appletvplus": "Apple TV", "apple_tv": "Apple TV", "apple": "Apple TV",
    "sky": "Sky", "sky cinema": "Sky Cinema", "sky cinema collection": "Sky Cinema",
    # "sky go" is the mobile app for the main Sky channels — label it "Sky", not a
    # separate service (it's just your Sky sub on a phone).
    "sky showcase": "Sky Showcase", "sky go": "Sky", "sky now": "Sky",
    "bbc": "BBC iPlayer", "bbc iplayer": "BBC iPlayer", "itv": "ITVX",
    "itvx": "ITVX", "channel 4": "Channel 4", "channel 4 odesly": "Channel 4",
    "channel 5": "Channel 5", "channel 5 odesly": "Channel 5",
    "prime video": "Prime Video", "amazon prime": "Prime Video", "amazon": "Prime Video",
    "paramount plus": "Paramount+", "paramount": "Paramount+",
    "hulu": "Hulu", "hulu uk": "Hulu", "crunchyroll": "Crunchyroll",
    "mgb pictures": "MGB Pictures", "magnolia": "Magnolia", "maverick": "Maverick",
    "universal": "Universal", "warner bros": "Warner Bros", "sony": "Sony",
    "a24": "A24", "studio canal": "Studio Canal", "britbox": "BritBox",
    "acorn tv": "Acorn TV", "mubi": "MUBI", "kanopy": "Kanopy", "shudder": "Shudder",
    "peacock": "Peacock", "pluto tv": "Pluto TV", "tubi": "Tubi",
    "freevee": "Freevee", "stan": "Stan", "binge": "Binge",
    "free": "Free TV", "free tv": "Free TV",
    "hbomax": "Max", "hbo max": "Max", "max": "Max", "warner bros discovery": "Max",
    "hbo max amazon channel": "Max (via Amazon)", "amazon channel": "Amazon Channel",
    "starzplay amazon channel": "StarzPlay (via Amazon)", "starz amazon channel": "StarzPlay (via Amazon)",
    "now tv cinema": "Sky Cinema", "now tv": "Sky", "now tv entertainment": "Sky",
    "sky store": "Sky Store", "apple tv store": "Apple TV", "itunes": "Apple TV",
    "amazon video": "Prime Video", "google play movies": "Google Play",
    "rakuten tv": "Rakuten TV", "youtube": "YouTube", "yahoo movies": "Yahoo",
    "samsung tv": "Samsung TV", "lg tv": "LG TV", "peacock uk": "Peacock",
}
SERVICE_ORDER = {"flatrate": 0, "rent": 1, "buy": 2, "free": 3}

# Numeric TMDB provider ids -> friendly UK names (the ids are stable).
PROVIDER_ID_NAMES = {
    8: "Netflix", 305: "Netflix", 327: "Netflix",
    303: "Disney+", 304: "Disney+", 258: "Disney+",
    314: "Apple TV", 315: "Apple TV", 316: "Apple TV", 2: "Apple TV",
    136: "Sky Cinema", 137: "Sky Cinema", 138: "Sky", 130: "Sky Store",
    # 139 (Sky Go) is the mobile *app* that streams the Sky main channels — show it as
    # "Sky" (it's the on-the-go version of your Sky sub), not a separate "Sky Go".
    139: "Sky", 140: "Sky", 141: "Sky", 142: "Sky",
    591: "Sky Cinema", 1899: "Max", 1825: "Max (via Amazon)", 1826: "Max (via Amazon)",
    38: "BBC iPlayer", 39: "BBC iPlayer", 528: "BBC iPlayer",
    60: "ITVX", 133: "ITVX", 134: "ITVX", 135: "ITVX",
    10: "Prime Video", 11: "Prime Video", 6: "Prime Video", 21: "Prime Video",
    331: "Paramount+", 332: "Paramount+",
    35: "Rakuten TV", 192: "YouTube", 21: "Amazon Video",
    34: "BritBox", 33: "BritBox", 26: "Acorn TV", 25: "MUBI",
    27: "Shudder", 36: "Shudder", 326: "HBO Max", 327: "Max",
}


# Channels that are genuinely free to watch (no subscription, no per-title cost):
# UK free-to-air + the always-free, ad-supported streaming services. Used by the
# "Free" filter. Subscription services (Netflix, Sky, Prime, Apple, …) deliberately
# do NOT count here — "free" means you don't pay at all, not "included in a sub".
FREE_CHANNELS = {"bbc", "itvx", "channel4", "channel5", "freevee", "pluto", "tubi"}


def _is_free(providers: list[dict]) -> bool:
    """True if the title is available on a genuinely-free service (see FREE_CHANNELS)."""
    return any(p.get("channel") in FREE_CHANNELS for p in (providers or []))


def _friendly_provider(slug: str, name: str) -> str:
    s = (slug or "").strip()
    if s.isdigit() and int(s) in PROVIDER_ID_NAMES:
        return PROVIDER_ID_NAMES[int(s)]
    sl = s.lower()
    if sl in PROVIDER_NAMES:
        return PROVIDER_NAMES[sl]
    n = (name or "").strip()
    return n or (sl.replace("_", " ").title() if sl else "")


def _parse_gb_watch(data: dict) -> list[dict]:
    """Map a watch/providers payload (v3 or v4 shape) onto our platform dict.
    Both versions use {provider_id, provider_name} under each availability bucket,
    so one parser serves both."""
    try:
        gb = (data.get("results") or {}).get("GB") or {}
    except Exception as e:  # noqa: BLE001
        log.debug("providers json parse failed: %s", e)
        return []
    out: dict[tuple, dict] = {}
    for stype, providers in gb.items():
        if not isinstance(providers, list):
            continue
        for p in providers:
            name = _friendly_provider(str(p.get("provider_id")), p.get("provider_name", ""))
            cid = channel_for(p.get("provider_id"), p.get("provider_name"))
            out[(name, stype)] = {
                "provider": name, "service": stype, "type": stype,
                "channel": cid, "channel_name": channel_meta(cid)["name"] if cid else name,
            }
    return list(out.values())


async def _providers(r: "Result") -> list[dict]:
    """Return [{provider, service, type}] for GB, deduped + priority-sorted.

    Uses TMDB watch/providers. The v3 key is tried first, then the v4 Read token, so
    the result always matches the key that produced the card's with_watch_provider
    pre-filter (keeps the ✓ badge and the detail list in agreement). Results are
    cached per title so re-selecting a service /
    re-searching is instant. Retries on rate-limit / transient errors."""
    if not r.tmdb_id or not (tmdb_key() or tmdb_v4_key()):
        return []
    ckey = (r.kind, r.tmdb_id)
    hit = _prov_cached(ckey)
    if hit is not None:
        return hit
    import asyncio as _aio
    tpath = "tv" if r.kind == "show" else "movie"
    url = f"{TMDB_BASE}/{tpath}/{r.tmdb_id}/watch/providers"
    # Try the v3 key first, then the v4 token. Consistency matters more than freshness
    # here: the same key is used for the search's with_watch_provider pre-filter, so the
    # card's ✓ badge and the detail's "where to watch" list always agree. (The v4 token
    # is fresher, but its provider data differs slightly — v3-first keeps them in sync.)
    if _ctx["v4_style"] is None:
        await _detect_v4_style()
    keys = []
    if tmdb_key():
        keys.append((tmdb_key(), "param"))
    if tmdb_v4_key():
        keys.append((tmdb_v4_key(), _ctx["v4_style"] or "param"))
    rr = None
    for k, style in keys:
        for attempt in range(3):
            try:
                rr = await _attach_auth(k, style, url, {"watch_region": "GB"})
                if rr.status_code == 200:
                    break
                if rr.status_code in (429, 500, 502, 503):
                    await _aio.sleep(0.4 * (attempt + 1))  # back off on rate-limit / transient
                    continue
                break  # 401/404 etc. -> try the next key
            except Exception as e:  # noqa: BLE001
                log.debug("providers attempt failed: %s", e)
                await _aio.sleep(0.4 * (attempt + 1))
        if rr is not None and rr.status_code == 200:
            break
        rr = None
    if rr is None or rr.status_code != 200:
        _PROV_CACHE[ckey] = (time.time(), None)  # cache the miss (short TTL)
        return []
    try:
        res = _parse_gb_watch(rr.json())
    except Exception as e:  # noqa: BLE001
        log.debug("providers parse failed: %s", e)
        res = []
    res.sort(key=lambda x: (SERVICE_ORDER.get(x["type"], 9), x["provider"]))
    res = res[:24]
    _PROV_CACHE[ckey] = (time.time(), res)
    _mark_cache_dirty()
    return res


async def _attach_providers(r: Result) -> None:
    """Fill r.platforms from TMDB watch/providers (v3 or v4 key)."""
    if not r.tmdb_id:
        return
    r.platforms = await _providers(r)


def _tpath(kind: str) -> str:
    return "tv" if kind == "show" else "movie"


async def _gather_bounded(coros, limit: int = 24, *, progress_key: str | None = None, progress_phase: str | None = None, progress_total: int | None = None, results: list | None = None, only_provided: bool = False, snap_every: int = 1) -> None:
    """Run coroutines with a cap on simultaneous HTTP calls (avoids rate-limit
    spikes when enriching a large pool). If a progress_key is given, reports a
    running count for the UI's live 'pulling N…' indicator. If `results` is the
    page being enriched, a growing snapshot of the completed items is exposed on
    the same progress channel (partial_results) so the client can render cards
    as they finish instead of waiting for the whole batch. With only_provided,
    the snapshot holds just the titles whose provider data has landed (used while
    the provider scan runs — the client hides platform-less cards behind a service
    lens anyway, and it keeps the polled payload small). snap_every throttles how
    often that snapshot is rebuilt (re-serialising a 600-title list on every one
    of 600 completions is needless CPU)."""
    sem = asyncio.Semaphore(limit)
    total = len(coros)
    done = 0
    t0 = progress_total or total
    # Never step backwards: if a prior phase already reported a higher count, keep it
    # (e.g. the provider scan for a service search shouldn't reset the bar the pool
    # already filled).
    prev = PROGRESS.get(progress_key, {}).get("done", 0) if progress_key else 0
    if progress_key:
        _progress(progress_key, phase=progress_phase or "working", done=prev, total=t0)
        if results is not None:
            rows = [r for r in results if r.platforms or not only_provided]
            _progress(progress_key, partial_results=[r.to_dict() for r in rows])

    async def run(c):
        nonlocal done
        async with sem:
            try:
                await c
            except Exception as e:  # noqa: BLE001
                log.debug("bounded gather: %s", e)
            finally:
                done += 1
                f = {"phase": progress_phase or "working", "done": max(prev, done), "total": t0}
                if results is not None and (done == total or done % max(1, snap_every) == 0):
                    rows = [r for r in results if r.platforms or not only_provided]
                    f["partial_results"] = [r.to_dict() for r in rows]
                if progress_key:
                    _progress(progress_key, **f)

    await asyncio.gather(*(run(c) for c in coros))
    if progress_key:
        _progress(progress_key, phase="done", done=max(prev, total), total=t0)


async def _resolve_tmdb_id(r: Result) -> None:
    """Map a non-TMDB result (TVMaze) onto a TMDB id so we can fetch providers.
    Uses the IMDb id if present (exact), else a title search. Best-effort."""
    if r.tmdb_id:
        return
    try:
        params: dict = {}
        if r.imdb_id:
            params["external_ids"] = r.imdb_id
        elif r.title:
            params["query"] = r.title
        else:
            return
        path = "search/tv" if r.kind == "show" else "search/movie"
        res = await _tmdb_get(f"{TMDB_BASE}/{path}", params)
        if res.status_code == 200:
            hits = res.json().get("results", [])
            pick = hits[0] if hits else None
            if pick and (not r.tmdb_id):
                r.tmdb_id = pick.get("id")
    except Exception as e:  # noqa: BLE001
        log.debug("resolve_tmdb_id failed: %s", e)


async def _attach_cast(r: Result) -> None:
    """Fill r.cast / r.cast_full from TMDB credits so cards and the detail view
    have cast without a second enrich round-trip (best-effort)."""
    if not r.tmdb_id or (r.cast and r.cast_full):
        return
    d = await _enrich_tmdb(r.tmdb_id, r.kind, for_cast=True)
    cast = [c.get("name") for c in d.get("credits", {}).get("cast", []) if c.get("name")][:12]
    if cast:
        r.cast = cast
        r.cast_full = [
            {"name": c.get("name"), "person_id": c.get("id"),
             "profile": (TMDB_IMG + c["profile_path"]) if c.get("profile_path") else None}
            for c in d.get("credits", {}).get("cast", []) if c.get("name")
        ][:12]
        if not r.directors:
            r.directors = [c.get("name") for c in d.get("credits", {}).get("crew", []) if c.get("job") == "Director"][:5]


async def _attach_rt(r: Result) -> None:
    """Fill r.ratings RT critic/audience (+ trailers/cast fallback) via the scrape.
    Only worth doing for the page the user sees; cached so re-sorts are free."""
    if not settings.rt_enabled or not r.title:
        return
    data = await _rt_lookup(r.title, r.year, r.kind)
    if not data:
        return
    cur = r.ratings or Ratings()
    r.ratings = Ratings(
        tmdb_vote=cur.tmdb_vote,
        tmdb_count=cur.tmdb_count,
        rt_tomatometer=data.get("rt_tomatometer") or cur.rt_tomatometer,
        rt_audience=data.get("rt_audience") or cur.rt_audience,
    )
    if not r.trailer_url and data.get("trailer"):
        r.trailer_url = f"https://www.youtube.com/embed/{data['trailer']}"


async def _enrich_tmdb(tmdb_id: int, kind: str, for_cast: bool = False) -> dict:
    """Fetch a title's TMDB detail (credits, videos, external_ids). `for_cast=True`
    caches the result per title (stable) so a 100-card browse doesn't re-fetch credits
    for the same titles on every re-search."""
    if for_cast:
        ckey = (kind, tmdb_id)
        hit = _CAST_CACHE.get(ckey)
        if hit and time.time() - hit[0] < _CAST_TTL:
            return hit[1]
    try:
        r = await _tmdb_get(f"{TMDB_BASE}/{_tpath(kind)}/{tmdb_id}", {"append_to_response": "credits,videos,external_ids"})
        r.raise_for_status()
        data = r.json()
    except Exception as e:  # noqa: BLE001
        log.debug("tmdb detail failed: %s", e)
        data = {}
    if for_cast:
        _CAST_CACHE[(kind, tmdb_id)] = (time.time(), data)
        _mark_cache_dirty()
    return data


# ---------------------------------------------------------------------------
# Filter / sort
# ---------------------------------------------------------------------------
def _apply_filters(results: list[Result], q: Query, *, early: bool = False, channels_prefiltered: bool = False) -> list[Result]:
    """Filter results.

    `early=True` runs only the provider/RT-independent checks (kind, year, language,
    genre, mood, text). It MUST run BEFORE the pool is capped and providers are looked
    up, otherwise unrelated titles crowd the real matches out of the capped pool — e.g.
    browsing "All" for a series-only mood (Reality TV) put the Films pool first, so the
    top `limit` were all non-Reality films and the reality shows never got provider data.
    The full pass (`early=False`) then applies the channel + rating filters once
    provider/RT data has been attached.
    """
    out, qwords, rtg = [], (q.q or "").lower().split(), (genres_for_mood(q.mood) if q.mood else [])
    for r in results:
        if q.kind != "any" and r.kind != q.kind:
            continue
        if q.year_min and (r.year or 0) < q.year_min:
            continue
        if q.year_max and (r.year or 9999) > q.year_max:
            continue
        # English-only: a title whose original language is known must be English.
        # Titles with no language recorded (e.g. TVMaze results) are kept — we can't
        # prove they're foreign, and the TMDB pools are already language-filtered.
        if q.english_only and (r.language or "").lower() not in ("", "en"):
            continue
        if q.genres and not any(g in r.genres for g in q.genres):
            continue
        if rtg and not any(g in r.genres for g in rtg):
            continue
        if qwords:
            hay = f"{r.title} {' '.join(r.cast)} {' '.join(r.directors)}".lower()
            if not all(w in hay for w in qwords):
                continue
        if not early:
            if q.min_rating and (r.ratings.tmdb_vote if r.ratings else None) is not None and (r.ratings.tmdb_vote or 0) < q.min_rating:
                continue
            if q.min_rt:
                rt = (r.ratings.rt_tomatometer if r.ratings else None)
                if rt is None or rt < q.min_rt:
                    continue
            if q.channels and not channels_prefiltered:
                # Compare by brand family so "Sky" also matches Sky Cinema / Showcase
                # (the sub-brands carry their own ids). A title matches if any of its
                # provider channels folds into one of the picked families.
                have = {channel_family(p.get("channel")) for p in (r.platforms or []) if p.get("channel")}
                want = {channel_family(c) for c in q.channels}
                if not have & want:
                    continue
        out.append(r)
    return out


# Sort keys the UI may request. Each maps to a (value-getter, descending) pair.
def _rt_val(r: Result, which: str) -> float | None:
    rt = r.ratings
    if not rt:
        return None
    v = rt.rt_tomatometer if which == "rt_critic" else rt.rt_audience
    return float(v) if v is not None else None


def _sort_key(r: Result, sort: str) -> tuple:
    """A sort key. Returns (primary, secondary) so ties fall back to title.
    Items with no value for a rating sort sink to the bottom."""
    has = lambda v: v is not None
    if sort == "title":
        return ((r.title or "").lower(),)
    if sort == "newest":
        return (-(r.year or 0),)
    if sort == "oldest":
        return ((r.year or 9999),)
    if sort == "votes":
        c = (r.ratings.tmdb_count if r.ratings else None) or 0
        return (-c,)
    if sort == "rt_critic":
        v = _rt_val(r, "rt_critic")
        return (0 if has(v) else 1, -(v or 0))
    if sort == "rt_audience":
        v = _rt_val(r, "rt_audience")
        return (0 if has(v) else 1, -(v or 0))
    if sort == "tmdb":
        v = (r.ratings.tmdb_vote if r.ratings else None)
        return (0 if has(v) else 1, -(v or 0))
    if sort in ("rating", "pct"):  # blend of every rating we have (0-10 or %), high first
        v = r.score()
        return (0 if has(v) else 1, -(v or 0))
    # relevance / unknown: leave as-is
    return (0,)


def _sort(results: list[Result], q: Query) -> list[Result]:
    if q.sort in ("relevance",):
        return results
    return sorted(results, key=lambda r: _sort_key(r, q.sort))


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------
@app.get("/api/keys")
async def get_keys():
    """Report which keys are set (masked) — for the in-app settings panel."""
    return _key_status()


@app.post("/api/keys")
async def set_keys(payload: dict):
    """Persist keys into backend/.env. Values may be empty to clear."""
    path = Path(__file__).resolve().parent.parent / ".env"
    vals = dict(settings.model_dump())
    for k in ("tmdb_api_key", "tmdb_v4_token", "serper_api_key"):
        if k in payload:
            vals[k] = (payload[k] or "").strip()
    lines = [
        f"SF_TMDB_API_KEY={vals['tmdb_api_key']}",
        f"SF_TMDB_V4_TOKEN={vals['tmdb_v4_token']}",
        f"SF_TVMAZE_ENABLED={str(vals.get('tvmaze_enabled', True)).lower()}",
        f"SF_RT_ENABLED={str(vals.get('rt_enabled', True)).lower()}",
        f"SF_SERPER_API_KEY={vals['serper_api_key']}",
    ]
    path.write_text("\n".join(lines) + "\n")
    # Reflect the change in this process immediately (no restart needed).
    settings.tmdb_api_key = vals.get("tmdb_api_key", "")
    settings.tmdb_v4_token = vals.get("tmdb_v4_token", "")
    settings.serper_api_key = vals.get("serper_api_key", "")
    # Validate whichever TMDB key changed so the user gets real feedback.
    validation = {}
    if "tmdb_api_key" in payload and vals.get("tmdb_api_key"):
        validation["api_key"] = await _validate_tmdb(vals["tmdb_api_key"])
    if "tmdb_v4_token" in payload and vals.get("tmdb_v4_token"):
        validation["v4_token"] = await _validate_tmdb(vals["tmdb_v4_token"])
    return {"ok": True, "validation": validation, **_key_status()}


def _mask(k: str) -> str:
    k = k or ""
    return f"{k[:4]}…{k[-4:]}" if len(k) > 12 else ("…" * 6 if k else "")


def _key_status() -> dict:
    return {
        "tmdb": {"set": bool(settings.tmdb_api_key), "masked": _mask(settings.tmdb_api_key)},
        "tmdb_v4": {"set": bool(settings.tmdb_v4_token), "masked": _mask(settings.tmdb_v4_token)},
        "serper": {"set": bool(settings.serper_api_key), "masked": _mask(settings.serper_api_key)},
        "rt_enabled": bool(settings.rt_enabled),  # backend global master switch (per-person rt_off is separate)
        "max_results": int(settings.max_results),  # ops: the "All" browse ceiling (SF_MAX_RESULTS)
    }


@app.get("/api/progress")
async def progress(pid: str | None = None):
    """Live search progress (phase + running counts) so the UI can show
    'pulling N…' while a slow channel/RT search runs."""
    # Drop entries older than 5 min to avoid unbounded growth.
    now = time.time()
    for k in [k for k, v in PROGRESS.items() if now - v.get("_t", 0) > 300]:
        PROGRESS.pop(k, None)
    if pid:
        p = PROGRESS.get(pid)
        if p:
            return {k: v for k, v in p.items() if k != "_t"}
    return {}


async def _validate_tmdb(key: str) -> dict:
    """Hit a trivial TMDB endpoint to confirm the key is actually accepted."""
    try:
        r1 = await CLIENT.get(f"{TMDB_BASE}/configuration", headers={"Authorization": f"Bearer {key}"})
        ok1 = r1.status_code == 200
        if not ok1:
            r2 = await CLIENT.get(f"{TMDB_BASE}/configuration", params={"api_key": key})
            if r2.status_code == 200:
                return {"ok": True, "status": 200, "message": "valid (v3 classic — films + some series)",
                        "key_type": "v3"}
            return {"ok": False, "status": r2.status_code, "message": f"rejected ({r2.status_code})"}
        return {"ok": True, "status": 200, "message": "valid (v4 — full film & series coverage)",
                "key_type": "v4"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": 0, "message": f"network: {e}"}


# Diagnostic log: the frontend POSTs here when it errors (e.g. a phone can't reach
# the backend), so we can read the real cause from the Mac instead of guessing.
DIAG: list[dict] = []


@app.post("/api/diag")
async def diag(request: Request):
    try:
        payload = await request.json()
        if not isinstance(payload, dict):
            payload = {"err": str(payload)[:600]}
    except Exception:
        raw = (await request.body()).decode("utf-8", "replace")[:600]
        payload = {"err": "unparseable body", "raw": raw}
    entry = dict(payload)
    entry["_t"] = time.time()
    DIAG.append(entry)
    DIAG[:] = DIAG[-50:]  # keep the last 50
    log.warning("DIAG: %s", entry)
    return {"ok": True}


@app.get("/api/diag")
async def diag_list():
    return DIAG[-20:]


@app.get("/api/meta")
async def meta():
    # TMDB is "configured" if EITHER key is present: the v3 "API Key" powers
    # search/discover/providers, and the v4 "Read Access Token" powers search +
    # full details (credits/trailers/ratings). Only a backend with *neither* needs
    # a key added.
    tmdb_configured = bool(tmdb_key() or tmdb_v4_key())
    return {
        "tmdb_key": tmdb_configured,
        "channels": all_channels(),
        "moods": MOODS,
        "genres": sorted({g for m in MOODS for g in m["genres"]}),
        "sources": [
            SourceStatus(key="tmdb", name="TMDB", enabled=settings.tmdb_enabled,
                          configured=tmdb_configured,
                          message="" if tmdb_configured else "add a free TMDB key in backend/.env to unlock movies"),
            SourceStatus(key="tvmaze", name="TVMaze", enabled=settings.tvmaze_enabled, configured=True),
        ],
    }

# The fully filtered + ranked result set for a search, kept briefly so "Load more" (offset>0)
# is just a slice of it. Without this every page re-ran the whole TMDB discover (the
# expensive part) and the free instance died on page 2.
_POOL_CACHE: dict[str, tuple[float, list]] = {}
_POOL_TTL = 15 * 60
_POOL_MAX = 8


def _pool_key(q) -> str:
    return json.dumps(q.model_dump(exclude={"offset", "limit"}), sort_keys=True, default=str)


async def _serve_page(q, ordered: list, pid: str | None) -> dict:
    """Slice one page (offset..offset+limit) out of the ranked pool and attach chips/cast."""
    limit = max(0, q.limit) if q.limit else 0
    limit = min(limit, settings.max_results)
    offset = max(0, q.offset)
    need_providers = bool(q.channels) or q.stream_only or q.free_only or q.where
    display_pool = ordered
    # Total the client *can* page through (the display pool size) — used to decide
    # whether a "load more" button should show. The returned `count` is the size of
    # THIS batch; the client tracks how many it has loaded and compares to `total`.
    total = len(display_pool)
    # Apply the "load more" offset: serve the next batch of the display pool.
    filtered = display_pool[offset:offset + limit] if limit > 0 else display_pool
    # Put the picked genres FIRST on every card: TMDB tags many titles with several
    # genres ("La Leyenda…" is Horror fourth, after Animation/Comedy/Family), so a
    # Horror browse otherwise shows cards that read "Animation, Comedy…" and look
    # like the filter leaked.
    wanted = {g.lower() for g in list(q.genres) + (genres_for_mood(q.mood) if q.mood else [])}
    if wanted:
        for r in filtered:
            r.genres.sort(key=lambda g: g.lower() not in wanted)
    if pid:
        _progress(pid, phase="finishing", done=0, total=len(filtered))
    # Enrich only the page the user sees (RT was just attached to the whole pool).
    if settings.tmdb_enabled and (tmdb_key() or tmdb_v4_key()):
        # results=filtered exposes a growing snapshot (partial_results) on the
        # progress channel, so the client can render each card as its cast and
        # where-to-watch data land — no need to wait for the whole batch.
        if len(filtered) > 80 or offset > 0:
            # Big page (or a "Load more" page: halve its TMDB calls on the slow free host) (an "All" browse): the grid never shows cast — the detail view
            # refetches it on tap via /api/enrich — so fetching full credits for every
            # card here is the single biggest cost of a big browse for data nobody
            # sees. Cards already streamed in during the provider scan above; just
            # fill any provider gaps (the channel path pre-attached them, so this is
            # usually a no-op) and let the detail view handle cast on demand.
            await _gather_bounded(
                [_attach_providers(r) for r in filtered if need_providers and not (r.platforms or [])],
                16, progress_key=pid, progress_phase="finishing", progress_total=len(filtered), results=filtered)
        else:
            await _gather_bounded(
                [asyncio.gather(*([_attach_providers(r)] if need_providers else []) + [_attach_cast(r)])
                 for r in filtered],
                16, progress_key=pid, progress_phase="finishing", progress_total=len(filtered), results=filtered)
    # "Free" (the user's meaning) = included in a service you already pay for, i.e. it
    # costs nothing *extra*. Kept when the title is on one of the user's ticked
    # subscriptions (any channel type — a free-to-air service you "have" also counts)
    # OR on a genuinely-free service (BBC/ITVX/Ch4/Ch5/Freevee/Pluto/Tubi). Rent/buy
    # per-title charges (Sky Store, Apple TV Store, Prime pay-movies) do NOT count.
    # Applied server-side (mirrors the client's household) so the returned page is
    # exactly the "no extra charge" set, even for services you have but aren't
    # filtering by.
    if q.free_only:
        hh = set(q.household)
        def _no_extra_charge(plats: list[dict]) -> bool:
            for p in plats or []:
                cid = p.get("channel")
                if cid is None:
                    continue
                # Genuinely-free service (BBC/ITVX/Ch4/Ch5/Freevee/Pluto/Tubi) = no cost
                # at all, always counts.
                if cid in FREE_CHANNELS:
                    return True
                # A service you have counts ONLY as a flatrate (subscription) entry —
                # being "on Apple/Prime/Sky" via rent/buy means you'd pay extra, which
                # is exactly what "free" must exclude.
                if p.get("type") == "flatrate" and cid in hh:
                    return True
            return False
        filtered = [r for r in filtered if _no_extra_charge(r.platforms)]
    if pid:
        PROGRESS.pop(pid, None)  # search done; let the frontend's poll finish
    return {"results": [r.to_dict() for r in filtered], "count": len(filtered), "total": total}



# ---------------------------------------------------------------------------
# True paging for service browses
#
# A plain service browse (optionally + genre/mood/year/English) is fully described by
# TMDB's own discover filters, so page N of the app is just the matching slice of TMDB's
# own paging — no need to download a big pool up front. That is what lets "Netflix" be
# the whole catalogue (thousands of titles) instead of the top ~240, while every request
# stays small (2-6 TMDB calls + one page of provider chips) on the free instance.
# ---------------------------------------------------------------------------
_TOTALS_CACHE: dict[str, tuple[float, int]] = {}
_TOTALS_TTL = 15 * 60
_TMDB_MAX_PAGE = 500          # TMDB refuses pages beyond this (10,000 results)
_CURSORS: dict[str, tuple[float, dict[str, int], list]] = {}   # (time, lane cursors, recently served ids)
_RECENT_IDS = 600      # remembered per chain so TMDB's unstable tie order can't re-serve a title
_WINDOW_EXTRA = 30     # fetch a few spare items per lane so skipped repeats don't leave a short page
_CURSOR_TTL = 30 * 60
_CURSOR_MAX = 300


def _browse_sort_key(sort: str):
    """Key over a raw TMDB discover item matching TMDB's own sort_by for `sort`."""
    def date(it): return it.get("release_date") or it.get("first_air_date") or ""
    def title(it): return (it.get("original_title") or it.get("original_name") or "").lower()
    if sort in ("rating", "tmdb"):
        return lambda it: (-(it.get("vote_average") or 0), -(it.get("popularity") or 0))
    if sort == "votes":
        return lambda it: (-(it.get("vote_count") or 0), -(it.get("popularity") or 0))
    if sort == "newest":
        return lambda it: (tuple(-ord(c) for c in date(it)) or (0,), -(it.get("popularity") or 0))
    if sort == "oldest":
        return lambda it: (date(it) or "9999", -(it.get("popularity") or 0))
    if sort == "title":
        return lambda it: (title(it),)
    return lambda it: (-(it.get("popularity") or 0),)


# App sort -> TMDB discover sort_by (movie, tv). Rating sorts also need a vote floor, or
# the top is full of obscure titles with a perfect score from a handful of votes.
_TMDB_SORTS: dict[str, tuple[str, str]] = {
    "relevance": ("popularity.desc", "popularity.desc"),
    "rating": ("vote_average.desc", "vote_average.desc"),
    "tmdb": ("vote_average.desc", "vote_average.desc"),
    "votes": ("vote_count.desc", "vote_count.desc"),
    "newest": ("primary_release_date.desc", "first_air_date.desc"),
    "oldest": ("primary_release_date.asc", "first_air_date.asc"),
    "title": ("original_title.asc", "original_name.asc"),
}
_RATING_VOTE_FLOOR = {"movie": "200", "show": "100"}


def _can_true_page(q: Query, prov_ids: list[int], tmdb_on: bool) -> bool:
    if not (tmdb_on and prov_ids and q.channels and not q.q):
        return False
    if q.sort not in _TMDB_SORTS or q.min_rt or q.free_only:
        return False        # these need data a discover page doesn't have -> old capped path
    names = list(q.genres) + (genres_for_mood(q.mood) if q.mood else [])
    gids = {_GENRE_IDS["movie"].get(normalize_genre(g) or g) for g in names}
    if _SPORTS_GENRE_ID in gids and len(gids) > 1:
        return False        # sports is a keyword, not a genre: don't mix it with real genres
    return True


def _browse_params(q: Query, kind: str, prov_ids: list[int]) -> dict[str, str] | None:
    """TMDB discover params for one kind, or None if this kind can't match the genres."""
    dkey = "primary_release_date" if kind == "movie" else "first_air_date"
    sort_by = _TMDB_SORTS.get(q.sort, _TMDB_SORTS["relevance"])[0 if kind == "movie" else 1]
    p: dict[str, str] = {"sort_by": sort_by, f"{dkey}.gte": "1950-01-01",
                         "with_watch_providers": "|".join(str(int(i)) for i in prov_ids),
                         "watch_region": "GB"}
    if q.sort in ("rating", "tmdb"):
        p["vote_count.gte"] = _RATING_VOTE_FLOOR[kind]
    if q.min_rating:
        # "Rated N+" is TMDB's own filter (whole catalogue, not just what's loaded). The vote
        # floor keeps a 9.5 from three votes out of a "90%+" list.
        p["vote_average.gte"] = str(float(q.min_rating))
        p["vote_count.gte"] = _RATING_VOTE_FLOOR[kind]
    if q.year_min:
        p[f"{dkey}.gte"] = f"{int(q.year_min)}-01-01"
    if q.year_max:
        p[f"{dkey}.lte"] = f"{int(q.year_max)}-12-31"
    if q.english_only:
        p["with_origin_language"] = "en"
    if q.channels and set(q.channels) <= UK_ONLY:
        p["with_origin_country"] = "GB"
    names = list(q.genres) + (genres_for_mood(q.mood) if q.mood else [])
    if names:
        gmap = _GENRE_IDS["movie" if kind == "movie" else "show"]
        gids = {gmap.get(normalize_genre(g) or g) for g in names} - {None}
        if _SPORTS_GENRE_ID in {gmap.get(normalize_genre(g) or g) for g in names} or \
           _SPORTS_GENRE_ID in gids:
            p["with_keywords"] = str(_SPORTS_KEYWORD)
            gids = {g for g in gids if g != _SPORTS_GENRE_ID}
        if gids:
            p["with_genres"] = "|".join(str(g) for g in sorted(gids))   # pipe = OR
        elif "with_keywords" not in p:
            return None     # asked for genres this kind doesn't have (e.g. Horror on TV)
    return p


async def _browse_fetch(path: str, params: dict[str, str], page: int) -> dict:
    r = await _tmdb_get(f"{TMDB_BASE}/{path}", {**params, "page": page})
    if r.status_code in (429, 500, 502, 503):
        await asyncio.sleep(0.6)
        r = await _tmdb_get(f"{TMDB_BASE}/{path}", {**params, "page": page})
    r.raise_for_status()
    return r.json()


async def _browse_page(q: Query, prov_ids: list[int], pid: str | None) -> dict:
    limit = min(max(q.limit, 0) or 60, settings.max_results)
    offset = max(0, q.offset)
    kinds = ["movie"] if q.kind == "movie" else (["show"] if q.kind == "show" else ["movie", "show"])
    # A "lane" is one TMDB discover list in the chosen order. Normally one per kind. For the
    # rating sorts each kind has TWO: well-voted titles first, then the thinly-voted rest
    # (also by rating) — so the whole catalogue is reachable, but a 10.0 from three votes
    # can never top the list.
    lanes: list[tuple[str, str, str, dict, int]] = []     # (name, kind, path, params, segment)
    for k in kinds:
        prm = _browse_params(q, k, prov_ids)
        if prm is None:
            continue
        path = "discover/movie" if k == "movie" else "discover/tv"
        lanes.append((k, k, path, prm, 0))
        if q.sort in ("rating", "tmdb") and not q.min_rating:
            floor = int(_RATING_VOTE_FLOOR[k])
            low = {kk: vv for kk, vv in prm.items() if kk != "vote_count.gte"}
            low["vote_count.lte"] = str(floor - 1)
            lanes.append((f"{k}_low", k, path, low, 1))
    if not lanes:
        return {"results": [], "count": 0, "total": 0}
    sem = asyncio.Semaphore(6)
    cache: dict[tuple, dict] = {}

    async def page(path: str, prm: dict, n: int) -> dict:
        key = (path, json.dumps(prm, sort_keys=True), n)
        if key not in cache:
            async with sem:
                cache[key] = await _browse_fetch(path, prm, n)
        return cache[key]

    totals: dict[str, int] = {}
    async def total_of(name, path, prm):
        tk = f"{name}|{path}|{json.dumps(prm, sort_keys=True)}"
        hit = _TOTALS_CACHE.get(tk)
        if hit and time.time() - hit[0] < _TOTALS_TTL:
            return hit[1]
        d = await page(path, prm, 1)
        t = min(int(d.get("total_results") or 0), _TMDB_MAX_PAGE * 20)
        _TOTALS_CACHE[tk] = (time.time(), t)
        return t
    got = await asyncio.gather(*(total_of(nm, path, prm) for nm, _, path, prm, _ in lanes))
    for (nm, _, _, _, _), t in zip(lanes, got):
        totals[nm] = t
    grand = sum(totals.values())
    if pid:
        _progress(pid, phase="pulled", done=0, total=limit)
    # Exact global order: each lane is already in the chosen order, so a page is a k-way
    # merge of the NEXT `limit` items from each lane, keeping the first `limit` by the sort
    # key (segment first, so well-voted titles always precede the thinly-voted ones). A
    # per-query cursor (how many of each lane are used) is remembered when a page is served,
    # so the next page continues exactly. With no cursor (first visit to that offset, e.g.
    # after a restart) it is estimated from the lanes' totals — only approximately ordered
    # at that one seam.
    qkey = _pool_key(q)
    names_l = [nm for nm, *_ in lanes]
    cur = None
    recent: list = []
    if offset == 0:
        cur = {nm: 0 for nm in names_l}
    else:
        hit = _CURSORS.get(f"{qkey}|{offset}")
        if hit and time.time() - hit[0] < _CURSOR_TTL:
            cur = dict(hit[1])
            recent = list(hit[2])
    if cur is None:
        cur = {nm: 0 for nm in names_l}
        left = offset
        for seg in (0, 1):
            ln = [(nm, totals[nm]) for nm, _, _, _, sg in lanes if sg == seg]
            tot = sum(t for _, t in ln)
            if not tot or left <= 0:
                continue
            take = min(left, tot)
            for nm, t in ln:
                cur[nm] = round(take * t / tot)
            left -= take
    sk = _browse_sort_key(q.sort)
    want = {channel_family(c) for c in q.channels}
    gnames = {g.lower() for g in list(q.genres) + (genres_for_mood(q.mood) if q.mood else [])}

    def on_service(r: Result) -> bool:
        # Same test the client applies, so a card the server returns is a card the client shows.
        return any(channel_family(p.get("channel")) in want for p in (r.platforms or []) if p.get("channel"))

    async def window_of(name, kind, path, prm, seg, seg0_left, need):
        if seg == 1 and seg0_left > need:
            return []          # the well-voted lanes still fill this page: don't touch the rest yet
        a, b = cur.get(name, 0), min(cur.get(name, 0) + need + _WINDOW_EXTRA, totals[name])
        if b <= a:
            return []
        pnums = [n for n in range(a // 20 + 1, (b - 1) // 20 + 2) if n <= _TMDB_MAX_PAGE]
        datas = await asyncio.gather(*(page(path, prm, n) for n in pnums), return_exceptions=True)
        out = []
        for n, d in zip(pnums, datas):
            if isinstance(d, Exception):
                try:
                    d = await page(path, prm, n)       # one retry (rate limit / blip)
                except Exception as exc:  # noqa: BLE001
                    log.debug("browse page raised %s", exc)
                    break       # stop here: never advance the cursor past a page we couldn't read
            for i, it in enumerate(d.get("results", [])):
                if a <= (n - 1) * 20 + i < b:
                    out.append((name, kind, seg, it))
        return out

    # Fill the page: titles TMDB lists but whose provider data doesn't confirm one of the
    # chosen services (rent/buy-only, lookup failed) are dropped, so keep pulling until the
    # page is full or the lanes run dry. The cursor advances over everything consumed.
    kept: list[Result] = []
    seen_ids: set = {tuple(x) for x in recent}
    consumed = 0
    for _round in range(10):
        short = limit - len(kept)
        if short <= 0:
            break
        need = max(short, 12)      # top-up rounds pull a few spare so a page isn't a title short; it may run slightly over
        seg0_left = sum(max(0, totals[nm] - cur.get(nm, 0)) for nm, _, _, _, sg in lanes if sg == 0)
        wins = await asyncio.gather(*(window_of(nm, kd, path, prm, sg, seg0_left, need) for nm, kd, path, prm, sg in lanes))
        cand = [x for w in wins for x in w]
        if not cand:
            break
        cand.sort(key=lambda t: (t[2],) + tuple(sk(t[3])))
        taken: list[tuple[str, dict]] = []
        used = {nm: 0 for nm in names_l}
        for nm, kd, sg, it in cand:
            if len(taken) >= need:
                break
            used[nm] += 1
            if (it.get("id"), kd) in seen_ids:
                continue
            seen_ids.add((it.get("id"), kd))
            recent.append([it.get("id"), kd])
            taken.append((kd, it))
        for nm in used:
            cur[nm] = cur.get(nm, 0) + used[nm]
        consumed += sum(used.values())
        batch = [_map_tmdb(it, k) for k, it in taken]
        if gnames:
            for r in batch:
                r.genres.sort(key=lambda g: g.lower() not in gnames)
        if pid:
            _progress(pid, phase="finishing", done=0, total=len(batch))
        await _gather_bounded([_attach_providers(r) for r in batch], 16,
                              progress_key=pid, progress_phase="finishing", progress_total=len(batch),
                              results=batch, only_provided=True, snap_every=10)
        kept.extend(r for r in batch if on_service(r))
    page_results = kept     # may be a few over `limit`: the cursor already moved past every title examined
    recent = recent[-_RECENT_IDS:]
    _CURSORS[f"{qkey}|{offset + consumed}"] = (time.time(), dict(cur), recent)
    while len(_CURSORS) > _CURSOR_MAX:
        _CURSORS.pop(next(iter(_CURSORS)))
    if pid:
        PROGRESS.pop(pid, None)
    # `next` = where the following page starts in the server's own ordering. The client must
    # use this (not its own card count, which loses cards to de-duplication).
    return {"results": [r.to_dict() for r in page_results], "count": len(page_results), "total": grand,
            "next": offset + consumed}


def _cancel_on_disconnect(fn):
    """Stop a request's work the moment the client hangs up.

    FastAPI keeps running an `async def` endpoint after the browser aborts (the user
    picked a different search). On the free-tier instance that abandoned search keeps
    eating TMDB calls/CPU and the NEW search queues behind it. This runs the handler as
    a task, polls for the disconnect, and cancels the task so the new search gets the
    whole instance. The route signature gains a `request` param so FastAPI injects it.
    """
    sig = inspect.signature(fn)
    req_param = inspect.Parameter("request", inspect.Parameter.KEYWORD_ONLY, annotation=Request)

    @functools.wraps(fn)
    async def wrapper(*args, request: Request, **kwargs):
        task = asyncio.ensure_future(fn(*args, **kwargs))
        try:
            while True:
                done, _ = await asyncio.wait({task}, timeout=0.5)
                if done:
                    return task.result()
                if await request.is_disconnected():
                    task.cancel()
                    log.warning("search cancelled: client disconnected")
                    return JSONResponse({"detail": "client closed request"}, status_code=499)
        except asyncio.CancelledError:
            task.cancel()
            raise

    wrapper.__signature__ = sig.replace(parameters=[*sig.parameters.values(), req_param])
    return wrapper


@app.get("/api/search")
@_cancel_on_disconnect
async def search(
    q: str | None = None,
    kind: str = "any",
    genres: list[str] = FQuery(default=[]),
    mood: str | None = None,
    year_min: int | None = None,
    year_max: int | None = None,
    min_rating: float | None = None,
    min_rt: int | None = None,
    channels: list[str] = FQuery(default=[], alias="channel"),
    where: bool = False,
    stream_only: bool = False,
    free_only: bool = False,
    english_only: bool = False,
    rt_off: bool = False,
    rt_on: bool = False,
    household: list[str] = FQuery(default=[]),
    fetch_all: bool = False,
    pool: int | None = None,
    sort: str = "relevance",
    limit: int = 60,
    offset: int = 0,
    progress_id: str | None = None,
):
    # Be lenient: the frontend may URL-encode a genre with a space oddly,
    # and FastAPI can reject the whole request on a stray value.
    genres = [g for g in genres if g and g.strip()]
    channels = [c for c in channels if c and c.strip()]
    household = [c for c in household if c and c.strip()]
    q = Query(q=q, kind=kind, genres=genres, mood=mood, year_min=year_min,
              year_max=year_max, min_rating=min_rating, min_rt=min_rt,
              channels=channels, where=where, stream_only=stream_only,
              free_only=free_only, english_only=english_only, rt_off=rt_off,
              rt_on=rt_on, household=household, fetch_all=fetch_all, pool=pool, sort=sort,
              limit=limit, offset=max(0, offset))
    # Rotten Tomatoes is OPT-IN: the scrape runs only when the client's own settings
    # have it enabled (rt_on). No flag from an old/unknown client means NO RT — the
    # slow "Reading ratings" step must never surprise anyone.
    # Without RT the RT/min-RT *filters* are no-ops too — with no scores there's
    # nothing to filter or sort by, so a title without a score isn't wrongly dropped.
    q.rt_on = False        # Rotten Tomatoes support was removed; ignore any old client that still asks
    if not q.rt_on:
        q.min_rt = None
        if q.sort in ("rt_critic", "rt_audience"):
            q.sort = "relevance"
    tmdb_on = settings.tmdb_enabled and bool(tmdb_key())
    pid = progress_id  # key for the live progress the frontend polls
    _pool_ck = _pool_key(q)
    if q.offset > 0:
        _hit = _POOL_CACHE.get(_pool_ck)
        if _hit and time.time() - _hit[0] < _POOL_TTL:
            return await _serve_page(q, _hit[1], pid)   # "Load more": a slice of the cached pool
    _bp_ids = provider_ids_for(q.channels) if (q.channels and tmdb_on) else []
    if _can_true_page(q, _bp_ids, tmdb_on):
        return await _browse_page(q, _bp_ids, pid)   # service browse: page straight through TMDB
    if pid:
        _progress(pid, phase="starting")
    results: list[Result] = []

    # When the user picks a specific service, only TMDB carries provider data,
    # so skip TVMaze (it has no where-to-watch) to keep results meaningful.
    tmdb_only = bool(q.channels)
    # Service pre-filter: TMDB's discover can filter by watch provider itself
    # (with_watch_providers, free) — the pool then only contains titles on the picked
    # services, and the expensive per-title provider scan is only needed for the
    # chips on displayed cards, not to build the result set.
    prov_ids = provider_ids_for(q.channels) if (q.channels and tmdb_on) else []
    # (Only the DISCOVER browse can carry the provider pre-filter — a text search
    # uses _tmdb_search, which has no such filter, so it keeps the scan+filter path.)
    # Channels TMDB doesn't track in GB (Channel 5) have no ids and can never appear
    # in any title's platforms, so they neither help nor hurt the pre-filter — the
    # lens of "my 11 services" prefilters over the 10 that have ids.
    prefiltered = bool(q.channels) and bool(prov_ids) and not q.q
    tasks: list[tuple] = []
    if q.q:
        if tmdb_on:
            tasks.append(("tmdb", q.kind, _tmdb_search(q.q, q.kind, q.english_only)))
        if q.kind in ("show", "any") and not tmdb_only and not q.english_only:
            # TVMaze has no origin-language filter, so an English-only search uses
            # TMDB alone (it filters with_origin_language=en); otherwise TVMaze backs
            # the shows side with zero config.
            tasks.append(("tvmaze", None, _tvmaze_search(q.q, q.q)))
        if not tasks:
            tasks.append(("tvmaze", None, _tvmaze_search(q.q)))
    else:
        if tmdb_on:
            # "any" = both movies AND shows (two pools, merged) so the All tab isn't
            # just films. movie-only / show-only use a single pool.
            dks = ["movie"] if q.kind == "movie" else (["show"] if q.kind == "show" else ["movie", "show"])
            # With a service filter we pull a bigger popularity pool, then attach
            # providers to find the titles actually on the picked service. A
            # UK-origin pool only for UK free-to-air services; subscription services
            # are global, so no origin filter (it would drop most foreign titles).
            oc = "GB" if q.channels and set(q.channels) <= UK_ONLY else None
            # fetch_all (service-filtered browse) uses a bigger pool so the provider
            # check finds more hits. The client asks for its max-results value; the
            # SF_FETCH_ALL_POOL setting caps memory use (1500 = safe on the free
            # 512MB Render instance; raise it on a bigger plan for deeper browses).
            base_pool = 1000 if q.channels else (200 if q.where else 120)
            if q.fetch_all and q.pool:
                base_pool = min(max(q.pool, 200), settings.fetch_all_pool)
            # Service browses cap the pull: non-prefiltered, only the top of the pool
            # can be provider-checked (2× the provider cap); prefiltered, the pool
            # must stay small because PAGE 1 pays for the whole discover (every 20
            # titles = one TMDB round-trip) — a 500 pool made page 1 take 124s and
            # the instance died under it. 240 = 12 discover calls + one chip page,
            # which fits the free host with room to spare.
            if q.channels:
                base_pool = min(base_pool, 240 if prefiltered else max(2 * settings.max_providers, 300))
            max_t = base_pool // len(dks)
            for dk in dks:
                tasks.append(("tmdb", dk, _tmdb_discover(dk, q.genres, year_min, year_max, max_t, oc, q.channels, pid, q.english_only, prov_ids)))
        if not tmdb_only and not q.english_only:
            # English-only browse: TVMaze has no language filter and its discover is
            # unfiltered, so it would dilute the pool — use the (language-filtered)
            # TMDB pools only.
            tasks.append(("tvmaze", None, _tvmaze_discover()))

    batched = await asyncio.gather(*(coro for _, _, coro in tasks), return_exceptions=True)
    # Interleave the sources round-robin instead of appending each pool whole. The
    # provider scan only checks the TOP of `results`, and movies-before-shows meant a
    # small scan (a mood browse at Max results = 100) probed films only and the shows
    # side never got checked. Interleaved, any prefix mixes both kinds, so the cap
    # spreads across films AND series.
    streams = [[_map_tmdb(it, kkind) if source == "tmdb" else _map_tvmaze(it) for it in chunk]
               for (source, kkind, _), chunk in zip(tasks, batched) if not isinstance(chunk, Exception)]
    i = 0
    while True:
        added = False
        for s in streams:
            if i < len(s):
                results.append(s[i])
                added = True
        if not added:
            break
        i += 1
    if pid:
        _progress(pid, phase="pulled", done=len(results), total=len(results))

    # When filtering by service, keep same-titled variants (e.g. the 1963, 2005 and
    # 2024 Doctor Who) — only one of them is on a given service, and we can't tell
    # which without provider data (fetched after dedup). Otherwise dedup as usual.
    keep_variants = bool(q.channels)
    seen: dict[tuple, Result] = {}
    for r in results:
        key = (r.title.lower(), r.kind)
        cur = seen.get(key)
        if cur is None:
            seen[key] = r
            continue
        if keep_variants:
            # keep both, but prefer the one with a poster/backdrop for display
            continue
        if (r.backdrop_url or r.overview) and not (cur.backdrop_url or cur.overview):
            seen[key] = r
        elif (r.ratings.tmdb_count if r.ratings and r.ratings.tmdb_count else 0) > (cur.ratings.tmdb_count if cur.ratings and cur.ratings.tmdb_count else 0):
            seen[key] = r
    results = list(seen.values())

    # Filter by genre/mood/year/language/text FIRST — before the pool is capped and
    # providers are looked up. These filters need no provider/RT data, and running them
    # here stops unrelated titles crowding the real matches out of the capped pool
    # (e.g. the Films pool pushing a series-only mood's shows past the cap). Also means
    # the provider lookup below only checks titles that can actually be shown.
    results = _apply_filters(results, q, early=True)

    # Cap the pool at the requested page size BEFORE the (expensive) provider
    # lookup: we only need provider data for the titles the user will actually
    # see. A 1000-result browse therefore costs ~100 provider calls, not ~1500.
    # limit 0 = "All" (no display cap). The backend still returns its full pool; the
    # client shows all of it. For provider/rating lookups we cap separately below.
    limit = max(0, q.limit) if q.limit else 0
    limit = min(limit, settings.max_results)
    offset = max(0, q.offset)
    # When the client is paging with "load more" (offset>0), it asks for its full
    # display pool (limit) and we serve a slice of it. Keep that whole pool ranked
    # (don't truncate here) so every offset is consistent — the expensive provider
    # lookup is still capped to the top `limit` below, so cost stays bounded.
    paging = offset > 0
    # RT / min-rating sorts need ratings to be meaningful, so keep the wider pool
    # for those; every other path is pre-capped to the page.
    rt_sort = q.sort in ("rt_critic", "rt_audience")
    if limit > 0 and not (q.channels or rt_sort or paging):
        results = _sort(results, q)[:limit]

    # Channel filter needs provider data, so attach it (in parallel) BEFORE the
    # other filters run — UNLESS TMDB already pre-filtered the pool by provider
    # (with_watch_provider), in which case every pool member is on a picked service
    # and the per-title scan is only ever needed for the displayed cards' chips.
    if q.channels and not prefiltered and settings.tmdb_enabled and (tmdb_key() or tmdb_v4_key()):
        # Only the top of the (service-pre-filtered, popularity-sorted) pool actually
        # survives to the page, so only THOSE need provider data. Capping here (instead
        # of scanning the whole ~1000 pool) is the main cold-start win: a 50-result
        # Netflix browse fetches ~50 provider lookups, not ~500.
        # Cap EVERY request at max_providers provider checks — not just "All" (the
        # client sends limit=1000 for an "All" browse, so keying the cap on limit==0
        # would never fire). Bigger limit = deeper pool, but the scan must still fit
        # inside one request on the free tier; the provider cache makes the next
        # browse or re-sort free.
        prov_limit = min(limit or len(results), settings.max_providers)
        prov_target = results[:prov_limit]
        if pid:
            _progress(pid, phase="providers", done=0, total=len(prov_target))
        await _gather_bounded([_resolve_tmdb_id(r) for r in prov_target if not r.tmdb_id])
        # results=prov_target + only_provided: cards stream onto the screen as each
        # title's provider data lands, instead of the screen sitting empty until the
        # whole scan finishes (the client hides platform-less cards behind a service
        # lens anyway, so this can't show anything that shouldn't be there).
        await _gather_bounded([_attach_providers(r) for r in prov_target if r.tmdb_id], 100,
                              progress_key=pid, progress_phase="providers", progress_total=len(prov_target),
                              results=prov_target, only_provided=True, snap_every=25)
    if rt_sort or q.min_rt or q.min_rating:
        # RT / min-rating filtering needs rating data: RT is attached to the whole
        # pool first (capped at 200 for speed; the cache makes re-sorts free), then
        # every filter (incl. the channel filter) runs against the fresh values.
        pool = results
        if settings.rt_enabled and q.rt_on and (q.min_rt or rt_sort):
            if pid:
                _progress(pid, phase="ratings", done=0, total=min(len(pool), 200))
            await _gather_bounded([_attach_rt(r) for r in pool[:200] if r.title], 10,
                                  progress_key=pid, progress_phase="ratings", progress_total=min(len(pool), 200))
        pool = _apply_filters(pool, q, channels_prefiltered=prefiltered)
    else:
        # Everything else (service filter, mood, year, genre, text) is pre-filtered
        # and provider-checked, so a single filter pass is exact.
        pool = _apply_filters(results, q, channels_prefiltered=prefiltered)
    # Prefiltered + ordinary sort: attach the displayed page's chips NOW, before the
    # RT pass, streamed to the screen. The RT pass publishes no partials, so doing
    # chips after it left a silent minute of "Nothing on <service>" even though the
    # result set was already known — the pool IS the answer on this path.
    if prefiltered and not rt_sort and not q.min_rt and not q.min_rating \
            and settings.tmdb_enabled and (tmdb_key() or tmdb_v4_key()):
        _pre_pool = _apply_filters(results, q, channels_prefiltered=True)
        _pre_sorted = _sort(_pre_pool, q)
        _pre_disp = _pre_sorted
        _pre_page = _pre_disp[offset:offset + limit] if limit > 0 else _pre_disp
        _wanted = {g.lower() for g in list(q.genres) + (genres_for_mood(q.mood) if q.mood else [])}
        if _wanted:
            for r in _pre_page:
                r.genres.sort(key=lambda g: g.lower() not in _wanted)
        if pid:
            _progress(pid, phase="finishing", done=0, total=len(_pre_page))
        await _gather_bounded([_attach_providers(r) for r in _pre_page], 16,
                              progress_key=pid, progress_phase="finishing", progress_total=len(_pre_page),
                              results=_pre_page, only_provided=True, snap_every=10)
    # RT for everything that survived, capped at 100 — and ONLY when the client's
    # settings have Rotten Tomatoes enabled (rt_on, opt-in). RT is a per-title scrape
    # (the slowest call we make) and an "All"-size service browse can leave a few
    # hundred survivors, so it must never run uninvited. Cards beyond the cap simply
    # show no rating until a later browse caches them. The cache makes repeats free.
    if settings.rt_enabled and q.rt_on:
        rt_pool = pool[:100]
        if pid:
            _progress(pid, phase="ratings", done=0, total=len(rt_pool))
        # 16-way: an All-size browse RT-scrapes up to 100 titles; at the old 10-way
        # that alone was a minute-plus on the free host. The cache makes repeats free.
        await _gather_bounded([_attach_rt(r) for r in rt_pool if r.title], 16,
                              progress_key=pid, progress_phase="ratings", progress_total=len(rt_pool))
    ordered = _sort(pool, q)
    # `limit` is a PAGE size: the whole (pool-capped) ordered set is the result, and
    # each request serves one offset..offset+limit slice of it. `total` tells the
    # client how many titles exist so "Load more" can keep fetching pages. (Older
    # clients that send their whole pool size as limit with no offset simply get the
    # first `limit` — same behaviour as before.)
    _POOL_CACHE[_pool_ck] = (time.time(), ordered)
    while len(_POOL_CACHE) > _POOL_MAX:
        _POOL_CACHE.pop(next(iter(_POOL_CACHE)))
    return await _serve_page(q, ordered, pid)


@app.post("/api/enrich")
async def enrich(payload: dict):
    title = payload.get("title", "")
    year, kind = payload.get("year"), payload.get("kind", "movie")
    # RT here too is opt-in: only scrape when the requesting device's settings
    # have it enabled (the client sends rt_on with the enrich body).
    rt = await _rt_lookup(title, year, kind) if (settings.rt_enabled and payload.get("rt_on")) else {}

    # If we have a TMDB id, pull full credits + trailer from TMDB (more reliable)
    tmdb_id = payload.get("tmdb_id")
    detail = await _enrich_tmdb(int(tmdb_id), kind) if tmdb_id and tmdb_key() else {}
    base = _map_tmdb_detail(detail, kind) if detail else Result(
        title=title, kind=kind, year=year, genres=payload.get("genres", []),
        poster_url=payload.get("poster_url"), backdrop_url=payload.get("backdrop_url"),
        overview=payload.get("overview"),
    )
    if not base.directors and rt.get("directors"):
        base.directors = rt["directors"]
    if not base.cast and rt.get("cast"):
        base.cast = rt["cast"]
    if not base.trailer_url and rt.get("trailer"):
        base.trailer_url = f"https://www.youtube.com/embed/{rt['trailer']}"
    # Trailer from TMDB (most reliable) if we still don't have one. Now routes via
    # the v4 token on the /3 base (the /4 base returns no videos), so this works again.
    if not base.trailer_url and tmdb_id and (tmdb_key() or tmdb_v4_key()):
        try:
            vid = _pick_trailer((await _tmdb_get(f"{TMDB_BASE}/{_tpath(kind)}/{tmdb_id}/videos")).json().get("results", []))
            if vid:
                base.trailer_url = f"https://www.youtube.com/embed/{vid['key']}"
        except Exception as e:  # noqa: BLE001
            log.debug("tmdb trailer failed: %s", e)
    pr = payload.get("ratings")
    cur = base.ratings or Ratings()
    ratings = Ratings(
        tmdb_vote=(pr.get("tmdb_vote") if isinstance(pr, dict) else None) or cur.tmdb_vote,
        tmdb_count=(pr.get("tmdb_count") if isinstance(pr, dict) else None) or cur.tmdb_count,
        rt_tomatometer=rt.get("rt_tomatometer"),
        rt_audience=rt.get("rt_audience"),
    )
    base.ratings = ratings
    base.platforms = await _providers(base)
    # The card already had providers (from the search pass). The refetch here can come
    # back thinner/empty (different key, transient, or a title the refetch key doesn't
    # track). Never *replace* what the card showed with an empty list — merge instead,
    # so the ✓ badge and the "where to watch" row always agree.
    prior = payload.get("platforms") or []
    if prior and len(base.platforms) < len(prior):
        seen = {(p.get("provider"), p.get("type")) for p in base.platforms}
        for p in prior:
            if (p.get("provider"), p.get("type")) not in seen:
                base.platforms.append(p)
                seen.add((p.get("provider"), p.get("type")))
    return base.to_dict()


_SIMILAR_CACHE: dict[str, tuple[float, list]] = {}
_SIMILAR_TTL = 6 * 3600


@app.get("/api/similar")
async def similar(tmdb_id: int, kind: str = "movie"):
    """'You may also like': TMDB recommendations for a title (similar as a fallback), each
    with where-to-watch chips so the app can mark the ones on the user's services."""
    ck = f"{kind}|{tmdb_id}"
    hit = _SIMILAR_CACHE.get(ck)
    if hit and time.time() - hit[0] < _SIMILAR_TTL:
        return {"results": hit[1]}
    if not (tmdb_key() or tmdb_v4_key()):
        return {"results": []}
    items: list[dict] = []
    for ep in ("recommendations", "similar"):
        try:
            d = (await _tmdb_get(f"{TMDB_BASE}/{_tpath(kind)}/{tmdb_id}/{ep}", {"page": 1})).json()
        except Exception as e:  # noqa: BLE001
            log.debug("tmdb %s failed: %s", ep, e)
            continue
        have = {it["id"] for it in items}
        items += [it for it in d.get("results", []) if it.get("id") not in have and it.get("poster_path")]
        if len(items) >= 12:
            break
    res = [_map_tmdb(it, "show" if kind == "show" else "movie") for it in items[:14]]
    await _gather_bounded([_attach_providers(r) for r in res], 8)
    out = [r.to_dict() for r in res]
    _SIMILAR_CACHE[ck] = (time.time(), out)
    while len(_SIMILAR_CACHE) > 200:
        _SIMILAR_CACHE.pop(next(iter(_SIMILAR_CACHE)))
    return {"results": out}


@app.post("/api/providers")
async def providers_batch(payload: dict):
    """Fresh where-to-watch for a list of saved titles (the watchlist): {items:[{tmdb_id,kind}]}
    -> {"<kind>:<tmdb_id>": [platforms]}. Uses the 24h provider cache, so it's cheap."""
    items = [it for it in (payload.get("items") or [])[:150] if it.get("tmdb_id")]
    rs = [Result(title="", kind="show" if it.get("kind") == "show" else "movie", tmdb_id=int(it["tmdb_id"])) for it in items]
    await _gather_bounded([_attach_providers(r) for r in rs], 8)
    return {f"{r.kind}:{r.tmdb_id}": r.platforms for r in rs}


@app.get("/api/person/{person_id}")
async def person(person_id: int):
    """A person's filmography (for the clickable-cast → their films/shows popup)."""
    if not tmdb_key():
        return {"name": "", "profile": None, "films": [], "shows": []}
    try:
        r = await _tmdb_get(f"{TMDB_BASE}/person/{person_id}",
                            {"append_to_response": "movie_credits,tv_credits", "language": "en-GB"})
        if r.status_code != 200:
            return {"name": "", "profile": None, "films": [], "shows": []}
        d = r.json()
        films = [
            {"title": m.get("title") or m.get("name"), "year": int((m.get("release_date") or m.get("first_air_date") or "")[:4]) if (m.get("release_date") or m.get("first_air_date")) else None,
             "role": m.get("character"), "poster": (TMDB_IMG + m["poster_path"]) if m.get("poster_path") else None,
             "tmdb_id": m.get("id"), "kind": "movie"}
            for m in d.get("movie_credits", {}).get("cast", []) if m.get("title")
        ]
        shows = [
            {"title": m.get("name"), "year": int((m.get("first_air_date") or "")[:4]) if m.get("first_air_date") else None,
             "role": m.get("character"), "poster": (TMDB_IMG + m["poster_path"]) if m.get("poster_path") else None,
             "tmdb_id": m.get("id"), "kind": "show"}
            for m in d.get("tv_credits", {}).get("cast", []) if m.get("name")
        ]
        films.sort(key=lambda x: -(x["year"] or 0))
        shows.sort(key=lambda x: -(x["year"] or 0))
        return {
            "name": d.get("name", ""),
            "profile": (TMDB_IMG + d["profile_path"]) if d.get("profile_path") else None,
            "films": films[:40], "shows": shows[:40],
        }
    except Exception as e:  # noqa: BLE001
        log.debug("person failed: %s", e)
        return {"name": "", "profile": None, "films": [], "shows": []}


# Serve the frontend so one command runs everything.
from pathlib import Path
from fastapi.responses import FileResponse

FRONTEND = Path(__file__).resolve().parent.parent.parent / "frontend" / "index.html"


FRONTEND_DIR = FRONTEND.parent


import httpx, time



@app.get("/")
async def index():
    if FRONTEND.exists():
        return FileResponse(FRONTEND, media_type="text/html")
    return {"hint": "frontend/index.html missing — run the backend separately on :8030"}


@app.get("/check")
async def check_page():
    p = FRONTEND_DIR / "check.html"
    if p.exists():
        return FileResponse(p, media_type="text/html")
    return {"hint": "check.html missing"}


# PWA: manifest + service worker + icon, served from the frontend directory.
# (The app is a single-file SPA, so a minimal SW that shells-caches makes it
# installable + launchable offline; live searches still need the network.)
@app.get("/manifest.webmanifest")
async def manifest():
    return FileResponse(FRONTEND_DIR / "manifest.webmanifest", media_type="application/manifest+json")


@app.get("/sw.js")
async def service_worker():
    return FileResponse(FRONTEND_DIR / "sw.js", media_type="application/javascript")


@app.get("/icon.svg")
async def icon():
    return FileResponse(FRONTEND_DIR / "icon.svg", media_type="image/svg+xml")


if __name__ == "__main__":
    import os
    import uvicorn
    host = os.environ.get("SF_HOST", "0.0.0.0")  # 0.0.0.0 = reachable on your network
    uvicorn.run(app, host=host, port=int(os.environ.get("SF_PORT") or os.environ.get("PORT") or "8030"))
