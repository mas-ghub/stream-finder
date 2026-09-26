"""FastAPI app — one-stop search/discover across streaming services.

Source of truth = TMDB (movies+shows: genres, cast, ratings, trailers).
TVMaze backs the shows side (works with zero config). Rotten Tomatoes enriches
with the Tomatometer, audience score, cast and trailer. Everything degrades
gracefully — with no TMDB key you still get shows via TVMaze.
"""
from __future__ import annotations

import asyncio
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

from .channels import UK_ONLY, all_channels, channel_for, channel_meta
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
    household: list[str] = Field(default_factory=list)
    fetch_all: bool = False
    pool: int | None = None
    sort: str = "relevance"
    limit: int = 60


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
# Rotten Tomatoes (scrape, no key) — Tomatometer / audience / cast / trailer
# ---------------------------------------------------------------------------
def _youtube_id(url: str) -> str | None:
    m = re.search(r"(?:watch\?v=|youtu\.be/|embed/)([\w-]{6,})", url)
    return m.group(1) if m else None


def _parse_rt(html: str) -> dict:
    out: dict = {}
    for m in re.finditer(r'<script type="application/ld\+json">(.*?)</script>', html, re.S):
        try:
            d = json.loads(m.group(1))
        except Exception:
            continue
        if not isinstance(d, dict) or d.get("@type") not in ("Movie", "TVSeries", "TVShow"):
            continue
        for per in d.get("director") if isinstance(d.get("director"), list) else []:
            if isinstance(per, dict) and per.get("name"):
                out.setdefault("directors", []).append(per["name"])
        for per in d.get("actor") if isinstance(d.get("actor"), list) else []:
            if isinstance(per, dict) and per.get("name") and len(out.get("cast", [])) < 8:
                out.setdefault("cast", []).append(per["name"])
        for v in d.get("video") if isinstance(d.get("video"), list) else []:
            if isinstance(v, dict) and v.get("url") and "youtu" in v["url"]:
                vid = _youtube_id(v["url"])
                if vid:
                    out.setdefault("trailer", vid)
        break
    for key in ("criticsScore", "audienceScore"):
        m = re.search(r'"' + key + r'":\{[^}]*?"averageRating"\s*:\s*"?([0-9.]+)', html)
        if m:
            out["rt_" + ("tomatometer" if key == "criticsScore" else "audience")] = min(100, int(float(m.group(1)) * 10))
    if "trailer" not in out:
        m = re.search(r"https?://(?:www\.|m\.)?youtube\.com/(?:watch\?v=|embed/)([\w-]{6,})|https?://youtu\.be/([\w-]{6,})", html)
        if m:
            out["trailer"] = m.group(1) or m.group(2)
    return out


async def _rt_resolve_url(title: str, kind: str, year: int | None) -> str | None:
    """Resolve the canonical RT URL for a title. Optional Serper key makes it
    reliable; without a key it best-efforts the slug and RT redirects if right."""
    serper = settings.serper_api_key
    if serper:
        try:
            r = await CLIENT.post(
                "https://google.serper.dev/search",
                headers={"X-API-KEY": serper, "Content-Type": "application/json"},
                json={"q": f"rottentomatoes {title} {year or ''}".strip()},
            )
            for res in r.json().get("organic", []):
                u = res.get("link", "")
                if "rottentomatoes.com" in u and re.search(r"/(m|t)/", u):
                    return u
        except Exception as e:  # noqa: BLE001
            log.debug("serper failed: %s", e)
    slug = re.sub(r"[^a-z0-9]+", "_", title.lower()).strip("_")
    prefix = "m" if kind == "movie" else "t"
    return f"https://www.rottentomatoes.com/{prefix}/{slug}"


# In-memory RT cache: scraping is the expensive part, so never re-scrape a title
# we already tried (a miss is cached too, briefly, to avoid hammering RT).
_RT_CACHE: dict[tuple, dict] = {}
_RT_MISS_TTL = 60  # seconds to remember a failed lookup before retrying


async def _rt_lookup(title: str, year: int | None, kind: str = "movie") -> dict:
    if not settings.rt_enabled or not title:
        return {}
    key = (title.lower(), year, kind)
    cached = _RT_CACHE.get(key)
    if cached is not None and cached.get("_t", 0) > time.time():
        return {k: v for k, v in cached.items() if not k.startswith("_")}
    url = await _rt_resolve_url(title, kind, year)
    data: dict = {}
    try:
        r = await CLIENT.get(url, headers={"Accept": "text/html"}, follow_redirects=True)
        if r.status_code == 200:
            data = _parse_rt(r.text)
    except Exception as e:  # noqa: BLE001
        log.debug("rt lookup failed for %s: %s", title, e)
    _RT_CACHE[key] = {**data, "_t": time.time() + (settings.cache_ttl if data else _RT_MISS_TTL)}
    return data


# ---------------------------------------------------------------------------
# TMDB fetch
# ---------------------------------------------------------------------------
async def _tmdb_search(q: str, kind: str) -> list[dict]:
    path = {"movie": "search/movie", "show": "search/tv", "any": "search/multi"}[kind]
    out: list[dict] = []
    for page in (1, 2):
        r = await _tmdb_get(f"{TMDB_BASE}/{path}", {"query": q, "include_adult": "false", "page": page})
        if r.status_code == 401:
            return []
        r.raise_for_status()
        out.extend(r.json().get("results", []))
    return out[:100]


async def _tmdb_discover_pages(path: str, params: dict[str, str], max_titles: int, origin_country: str | None = None, progress_id: str | None = None) -> list[dict]:
    """Page through one discover query (single genre, or no genre) up to max_titles.
    A 401 (bad key) short-circuits the whole search, so it aborts early."""
    p = dict(params)
    if origin_country:
        p["with_origin_country"] = origin_country
        p["sort_by"] = "popularity.desc"
        p["vote_count.gte"] = "10"
    out: list[dict] = []
    pages = min((max_titles + 19) // 20, 30)  # 20 per page, cap ~600
    for page in range(1, pages + 1):
        r = await _tmdb_get(f"{TMDB_BASE}/{path}", {**p, "page": page})
        if r.status_code == 401:
            return []
        r.raise_for_status()
        data = r.json()
        batch = data.get("results", [])
        out.extend(batch)
        if progress_id:
            _progress(progress_id, phase="pulled", done=len(out), total=max_titles)
        if len(out) >= max_titles or page >= data.get("total_pages", 1) or not batch:
            break
    return out


async def _tmdb_discover(kind: str, genres: list[str], year_min: int | None = None, year_max: int | None = None, max_titles: int = 120, origin_country: str | None = None, channels: list[str] | None = None, progress_id: str | None = None) -> list[dict]:
    gids = sorted({_GENRE_IDS[kind].get(normalize_genre(g) or g) for g in genres} - {None})
    path = "discover/movie" if kind == "movie" else "discover/tv"
    dkey = "primary_release_date" if kind == "movie" else "first_air_date"
    base: dict[str, str] = {"sort_by": "vote_average.desc", "vote_count.gte": "150", f"{dkey}.gte": "1950-01-01"}
    if year_min:
        base[f"{dkey}.gte"] = f"{int(year_min)}-01-01"
    if year_max:
        base[f"{dkey}.lte"] = f"{int(year_max)}-12-31"
    # TMDB's `with_genres` is AND (a title must carry every listed genre), so picking
    # several genres/moods collapses the pool to near-empty. We want OR (union): pull
    # each selected genre separately, then merge + dedup. One genre -> a single query
    # (no extra requests). No genres -> one unrestricted query.
    buckets = [dict(base, with_genres=str(g)) for g in gids] or [dict(base)]
    if progress_id:
        _progress(progress_id, phase="pulled", done=0, total=max_titles)
    per_bucket = max(40, max_titles // len(buckets))  # keep total requests bounded
    chunks = await asyncio.gather(*[
        _tmdb_discover_pages(path, params, per_bucket, origin_country, progress_id)
        for params in buckets
    ], return_exceptions=True)
    seen: dict[int, dict] = {}
    for chunk in chunks:
        if isinstance(chunk, Exception):
            log.debug("discover bucket raised %s", chunk)
            continue
        for it in chunk:
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
    "sky showcase": "Sky Showcase", "sky go": "Sky Go", "sky now": "Sky",
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
    139: "Sky Showcase", 140: "Sky", 141: "Sky", 142: "Sky",
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
    return res


async def _attach_providers(r: Result) -> None:
    """Fill r.platforms from TMDB watch/providers (v3 or v4 key)."""
    if not r.tmdb_id:
        return
    r.platforms = await _providers(r)


def _tpath(kind: str) -> str:
    return "tv" if kind == "show" else "movie"


async def _gather_bounded(coros, limit: int = 24, *, progress_key: str | None = None, progress_phase: str | None = None, progress_total: int | None = None) -> None:
    """Run coroutines with a cap on simultaneous HTTP calls (avoids rate-limit
    spikes when enriching a large pool). If a progress_key is given, reports a
    running count for the UI's live 'pulling N…' indicator."""
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

    async def run(c):
        nonlocal done
        async with sem:
            try:
                await c
            except Exception as e:  # noqa: BLE001
                log.debug("bounded gather: %s", e)
            finally:
                done += 1
                if progress_key:
                    _progress(progress_key, phase=progress_phase or "working", done=max(prev, done), total=t0)

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
    return data


# ---------------------------------------------------------------------------
# Filter / sort
# ---------------------------------------------------------------------------
def _apply_filters(results: list[Result], q: Query) -> list[Result]:
    out, qwords, rtg = [], (q.q or "").lower().split(), (genres_for_mood(q.mood) if q.mood else [])
    for r in results:
        if q.kind != "any" and r.kind != q.kind:
            continue
        if q.year_min and (r.year or 0) < q.year_min:
            continue
        if q.year_max and (r.year or 9999) > q.year_max:
            continue
        if q.min_rating and (r.ratings.tmdb_vote if r.ratings else None) is not None and (r.ratings.tmdb_vote or 0) < q.min_rating:
            continue
        if q.min_rt:
            rt = (r.ratings.rt_tomatometer if r.ratings else None)
            if rt is None or rt < q.min_rt:
                continue
        if q.genres and not any(g in r.genres for g in q.genres):
            continue
        if rtg and not any(g in r.genres for g in rtg):
            continue
        if qwords:
            hay = f"{r.title} {' '.join(r.cast)} {' '.join(r.directors)}".lower()
            if not all(w in hay for w in qwords):
                continue
        if q.channels:
            have = {p.get("channel") for p in (r.platforms or []) if p.get("channel")}
            if not have & set(q.channels):
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
            SourceStatus(key="rt", name="Rotten Tomatoes", enabled=settings.rt_enabled, configured=True),
        ],
    }


@app.get("/api/search")
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
    household: list[str] = FQuery(default=[]),
    fetch_all: bool = False,
    pool: int | None = None,
    sort: str = "relevance",
    limit: int = 60,
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
              free_only=free_only, household=household, fetch_all=fetch_all,
              pool=pool, sort=sort, limit=limit)
    tmdb_on = settings.tmdb_enabled and bool(tmdb_key())
    pid = progress_id  # key for the live progress the frontend polls
    if pid:
        _progress(pid, phase="starting")
    results: list[Result] = []

    # When the user picks a specific service, only TMDB carries provider data,
    # so skip TVMaze (it has no where-to-watch) to keep results meaningful.
    tmdb_only = bool(q.channels)
    tasks: list[tuple] = []
    if q.q:
        if tmdb_on:
            tasks.append(("tmdb", q.kind, _tmdb_search(q.q, q.kind)))
        if q.kind in ("show", "any") and not tmdb_only:
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
            max_t = base_pool // len(dks)
            for dk in dks:
                tasks.append(("tmdb", dk, _tmdb_discover(dk, q.genres, year_min, year_max, max_t, oc, q.channels, pid)))
        if not tmdb_only:
            tasks.append(("tvmaze", None, _tvmaze_discover()))

    batched = await asyncio.gather(*(coro for _, _, coro in tasks), return_exceptions=True)
    for (source, kkind, _), chunk in zip(tasks, batched):
        if isinstance(chunk, Exception):
            log.debug("source %s raised %s", source, chunk)
            continue
        for it in chunk:
            results.append(_map_tmdb(it, kkind) if source == "tmdb" else _map_tvmaze(it))
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

    # Cap the pool at the requested page size BEFORE the (expensive) provider
    # lookup: we only need provider data for the titles the user will actually
    # see. A 1000-result browse therefore costs ~100 provider calls, not ~1500.
    limit = min(max(1, q.limit), settings.max_results)
    # RT / min-rating sorts need ratings to be meaningful, so keep the wider pool
    # for those; every other path is pre-capped to the page.
    rt_sort = q.sort in ("rt_critic", "rt_audience")
    if not (q.channels or rt_sort):
        results = _sort(results, q)[:limit]

    # Channel filter needs provider data, so attach it (in parallel) BEFORE the
    # other filters run. Otherwise the RT/min-rating filters (which drop titles
    # with no rating yet) would shrink the pool before the channel filter sees it.
    if q.channels and settings.tmdb_enabled and (tmdb_key() or tmdb_v4_key()):
        if pid:
            _progress(pid, phase="providers", done=0, total=len(results))
        await _gather_bounded([_resolve_tmdb_id(r) for r in results if not r.tmdb_id])
        await _gather_bounded([_attach_providers(r) for r in results if r.tmdb_id], 100,
                              progress_key=pid, progress_phase="providers", progress_total=len(results))
    if rt_sort or q.min_rt or q.min_rating:
        # RT / min-rating filtering needs rating data: RT is attached to the whole
        # pool first (capped at 200 for speed; the cache makes re-sorts free), then
        # every filter (incl. the channel filter) runs against the fresh values.
        pool = results
        if settings.rt_enabled and (q.min_rt or rt_sort):
            if pid:
                _progress(pid, phase="ratings", done=0, total=min(len(pool), 200))
            await _gather_bounded([_attach_rt(r) for r in pool[:200] if r.title], 10,
                                  progress_key=pid, progress_phase="ratings", progress_total=min(len(pool), 200))
        pool = _apply_filters(pool, q)
    else:
        # Everything else (service filter, mood, year, genre, text) is pre-filtered
        # and provider-checked, so a single filter pass is exact.
        pool = _apply_filters(results, q)
    # RT for EVERY title that survived (no cap), so the client can sort/filter by
    # RT locally and the result set is identical no matter the pool order or which
    # device made the request. A ~200-title service browse is a few seconds; the
    # provider/rating cache makes repeat queries free.
    if settings.rt_enabled:
        if pid:
            _progress(pid, phase="ratings", done=0, total=len(pool))
        await _gather_bounded([_attach_rt(r) for r in pool if r.title], 10,
                              progress_key=pid, progress_phase="ratings", progress_total=len(pool))
    ordered = _sort(pool, q)
    filtered = ordered[:limit]
    if pid:
        _progress(pid, phase="finishing", done=0, total=len(filtered))
    # Enrich only the page the user sees (RT was just attached to the whole pool).
    need_providers = bool(q.channels) or q.stream_only or q.free_only or q.where
    if settings.tmdb_enabled and (tmdb_key() or tmdb_v4_key()):
        await _gather_bounded(
            [asyncio.gather(*([_attach_providers(r)] if need_providers else []) + [_attach_cast(r)])
             for r in filtered],
            8, progress_key=pid, progress_phase="finishing", progress_total=len(filtered))
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
    return {"results": [r.to_dict() for r in filtered], "count": len(filtered)}


@app.post("/api/enrich")
async def enrich(payload: dict):
    title = payload.get("title", "")
    year, kind = payload.get("year"), payload.get("kind", "movie")
    rt = await _rt_lookup(title, year, kind)

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
