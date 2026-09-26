"""Canonical list of UK streaming services + the provider aliases (TMDB names,
ids, brand slugs) that fold into each. Used to map a title's where-to-watch
providers onto the user's household subscriptions.

The UI lets the user tick which ones they have (and add their own). A title is
"free for you" if any provider it's on matches a service the user has AND that
provider is a subscription (flatrate) — not rent/buy.
"""
from __future__ import annotations

# id -> {name, color, free_default}
# free_default = True means the service is usually "included" (no extra per-title cost)
CHANNELS: dict[str, dict] = {
    "netflix": {"name": "Netflix", "color": "#E50914"},
    "disney": {"name": "Disney+", "color": "#113CCF"},
    "apple": {"name": "Apple TV", "color": "#5E5CE6"},
    "sky": {"name": "Sky", "color": "#0A1E5B"},
    # Sub-brands of Sky / duplicate Prime ids still map for matching, but are
    # hidden from the picker (visible=False) to keep it uncluttered.
    "sky_cinema": {"name": "Sky Cinema", "color": "#0A1E5B", "visible": False},
    "sky_showcase": {"name": "Sky Showcase", "color": "#0A1E5B", "visible": False},
    "sky_go": {"name": "Sky Go", "color": "#0A1E5B", "visible": False},
    "prime": {"name": "Prime Video", "color": "#00A8E1"},
    "prime_video": {"name": "Prime Video", "color": "#00A8E1", "visible": False},
    "paramount": {"name": "Paramount+", "color": "#17083C"},
    "hulu": {"name": "Hulu", "color": "#1CE783"},
    "bbc": {"name": "BBC iPlayer", "color": "#A31BBB"},
    "itvx": {"name": "ITVX", "color": "#0B1B3D"},
    "channel4": {"name": "Channel 4", "color": "#051C33"},
    "channel5": {"name": "Channel 5", "color": "#FF5C00"},
    "max": {"name": "Max", "color": "#00267E"},
    "amazon": {"name": "Amazon Channel", "color": "#4B4B4B"},
    "britbox": {"name": "BritBox", "color": "#002B7F"},
    "acorn": {"name": "Acorn TV", "color": "#1D3C5F"},
    "mubi": {"name": "MUBI", "color": "#000000"},
    "shudder": {"name": "Shudder", "color": "#111111"},
    "crunchyroll": {"name": "Crunchyroll", "color": "#F47521"},
    "pluto": {"name": "Pluto TV", "color": "#250F73"},
    "tubi": {"name": "Tubi", "color": "#FF6A00"},
    "freevee": {"name": "Freevee", "color": "#0064FF"},
    "starzplay": {"name": "StarzPlay", "color": "#7A2D9C"},
    "encore": {"name": "Encore", "color": "#333333"},
    "rakuten": {"name": "Rakuten TV", "color": "#FF4C00"},
}

# Aliases (lowercased) -> channel id. TMDB provider names + ids + brand slugs.
_ALIASES: dict[str, str] = {}
for cid, meta in CHANNELS.items():
    _ALIASES[meta["name"].lower()] = cid
    _ALIASES[cid] = cid

_EXTRA = {
    # Netflix
    "netflix": "netflix",
    # Disney (Disney+, Star, National Geographic)
    "disney+": "disney", "disney plus": "disney", "star": "disney",
    "national geographic": "disney", "nat geo": "disney",
    # Apple (Apple TV+ subscription + Apple TV Store rent/buy)
    "apple tv+": "apple", "apple tv plus": "apple", "appletvplus": "apple",
    "apple tv": "apple", "apple": "apple",
    # NOTE: "apple tv store" / "itunes" are deliberately NOT folded into "apple" —
    # they are rent/buy stores, not your Apple TV+ subscription. (Folding them made
    # a title's rent/buy Apple entry count as "free via your Apple sub" — it isn't.)
    # Sky family — user has "Sky everything", so any Sky maps to sky; sky_cinema is
    # a sub but also folded to sky for the "do I have it" test (they have all Sky).
    # "sky store" is a rent/buy store, NOT folded into "sky".
    "sky cinema": "sky_cinema", "sky cinema collection": "sky_cinema",
    "now tv cinema": "sky_cinema", "now tv": "sky",
    "sky showcase": "sky_showcase", "sky go": "sky_go", "sky": "sky",
    # Prime (the user's free-with-Prime subscription). "Prime Video" is the
    # on-demand video service included with Amazon Prime, so it folds into prime.
    # "amazon video" is a rent/buy store, NOT folded into "prime".
    "amazon prime": "prime", "prime": "prime", "prime video": "prime",
    "amazon": "prime",
    # Paramount, Hulu, Max. Max-via-Amazon is a SEPARATE paid channel, NOT Prime.
    "paramount+": "paramount", "paramount plus": "paramount", "paramount": "paramount",
    "hulu": "hulu", "hulu uk": "hulu",
    "max": "max", "hbo max": "max", "hbomax": "max", "warner bros discovery": "max",
    "hbo max amazon channel": "amazon", "amazon channel": "amazon",
    "starzplay amazon channel": "amazon", "starz amazon channel": "amazon",
    # UK free/public
    "bbc iplayer": "bbc", "bbc": "bbc", "itvx": "itvx", "itv x": "itvx", "itv": "itvx",
    "channel 4": "channel4", "channel 4 odesly": "channel4", "4od": "channel4",
    "channel 5": "channel5", "channel 5 odesly": "channel5",
    # others
    "britbox": "britbox", "acorn tv": "acorn", "mubi": "mubi", "shudder": "shudder",
    "crunchyroll": "crunchyroll", "pluto tv": "pluto", "tubi": "tubi",
    "freevee": "freevee", "starzplay": "starzplay", "rakuten tv": "rakuten",
    "encore": "encore",
}
_ALIASES.update(_EXTRA)

# Numeric TMDB provider ids -> channel id (stable, most reliable).
ID_TO_CHANNEL: dict[int, str] = {
    8: "netflix", 305: "netflix", 327: "netflix",
    303: "disney", 304: "disney", 258: "disney", 88: "disney",
    314: "apple", 315: "apple", 316: "apple", 2: "apple",
    136: "sky_cinema", 137: "sky_cinema", 138: "sky", 130: "sky",
    139: "sky_showcase", 140: "sky", 141: "sky", 142: "sky", 591: "sky_cinema",
    10: "prime", 11: "prime", 21: "prime", 6: "prime",
    331: "paramount", 332: "paramount",
    38: "bbc", 39: "bbc", 528: "bbc",
    60: "itvx", 133: "itvx", 134: "itvx", 135: "itvx",
    # Max: standalone. Max-via-Amazon (1825) is a separate paid channel.
    1899: "max", 1826: "max", 326: "max", 327: "max", 1825: "amazon",
    34: "britbox", 33: "britbox", 26: "acorn", 25: "mubi",
    27: "shudder", 36: "shudder", 35: "rakuten",
}


def channel_for(provider_id: str | int | None, provider_name: str | None) -> str | None:
    """Map a TMDB provider onto a canonical channel id, or None if untracked.

    Rent/buy *store* names (Apple TV Store, Amazon Video, Sky Store, iTunes, …) are
    NOT folded into the subscription channel — they would otherwise make a title's
    rent/buy entry count as "free via the sub" and hide the user's real sub chip.
    """
    name = (provider_name or "").strip().lower()
    if any(s in name for s in (" store", "itunes", "amazon video", "amazon channel")):
        return None
    pid = str(provider_id or "").strip()
    if pid.isdigit() and int(pid) in ID_TO_CHANNEL:
        return ID_TO_CHANNEL[int(pid)]
    if pid and pid.lower() in _ALIASES:
        return _ALIASES[pid.lower()]
    if not name:
        return None
    if name in _ALIASES:
        return _ALIASES[name]
    # substring fallback for the big ones
    for needle in ("netflix", "disney", "apple", "prime", "paramount", "hulu",
                   "max", "bbc", "itv", "channel 4", "channel 5", "sky"):
        if needle in name:
            if needle == "apple":
                return "apple"
            if needle in ("channel 4",):
                return "channel4"
            if needle in ("channel 5",):
                return "channel5"
            if needle in ("bbc",):
                return "bbc"
            if needle in ("itv",):
                return "itvx"
            if needle in ("sky",):
                return "sky"
            if needle in ("prime",):
                return "prime"
            return _ALIASES.get(needle)
    return None


# Numeric provider ids that are flatrate (streamable) for each channel.
# Inverse of ID_TO_CHANNEL, plus a few "… with Ads" / "… Channel" variant ids that
# TMDB reports as flatrate for the same service (so the discover `with_watch_provider`
# filter matches what the per-title `_providers` mapping will later confirm).
PROVIDER_IDS: dict[str, list[int]] = {
    "netflix": [8, 305, 327, 1796],
    "disney": [303, 304, 258, 88, 337],
    "apple": [314, 315, 316, 2],
    "prime": [10, 11, 21, 6, 9],
    "paramount": [331, 332],
    "max": [1899, 1826, 326],
    "amazon": [1825],
    "sky": [138, 140, 141, 130, 591, 136, 137, 139],
    "itvx": [60, 133, 134, 135, 2300],
    "bbc": [38, 39, 528],
    "channel4": [],   # free-to-air; surfaced via the per-title provider pass, not discover
    "channel5": [],   # free-to-air; surfaced via the per-title provider pass, not discover
    "britbox": [34, 33],
    "acorn": [26],
    "mubi": [25, 11, 201],
    "shudder": [27, 36],
    "rakuten": [35],
    "hulu": [310],
    "tubi": [1789],
    "pluto": [339],
    "freevee": [338],
    "starzplay": [328],
    "encore": [329],
}


def provider_ids_for(channels: list[str]) -> list[int]:
    """Unique flatrate provider ids for the given channel ids (OR-able by TMDB)."""
    seen: list[int] = []
    for cid in channels or []:
        for pid in PROVIDER_IDS.get(cid, []):
            if pid not in seen:
                seen.append(pid)
    return seen


# UK-only free-to-air services. Their catalogues are British, so a UK-origin filter
# is needed to surface them at all. Subscription services (Netflix, Disney, Sky,
# Prime…) carry global catalogues, so they must NOT be origin-filtered or most of
# their horror/action/etc. (foreign-made) titles are dropped.
UK_ONLY = {"bbc", "itvx", "channel4", "channel5"}


def channel_meta(cid: str) -> dict:
    return CHANNELS.get(cid, {"name": cid.replace("_", " ").title(), "color": "#334155"})


def all_channels() -> list[dict]:
    out = []
    for cid, meta in CHANNELS.items():
        if meta.get("visible") is False:
            continue
        out.append({"id": cid, **{k: v for k, v in meta.items() if k != "visible"}})
    return out


def match(providers: list[dict], household: list[str]) -> dict:
    """Given a title's providers and the user's ticked channel ids, classify it.

    Returns:
      status: "free" (a service they have, included/subscription) |
              "rent" (only rent/buy) | "none" (nothing tracked)
      free_channels: [channel ids they have, flatrate]
      subs: [{channel, channel_name, color}]  # they have + it's a sub
      rent_buy: [channel names]               # rent/buy anywhere
    """
    have = set(household or [])
    free: list[str] = []
    subs: list[dict] = []
    rent_buy: list[str] = []
    for p in providers or []:
        cid = p.get("channel")
        if p.get("type") == "flatrate" and cid and cid in have:
            if cid not in free:
                free.append(cid)
            m = channel_meta(cid)
            if not any(s["channel"] == cid for s in subs):
                subs.append({"channel": cid, "channel_name": m["name"], "color": m["color"]})
        elif p.get("type") in ("rent", "buy") and p.get("provider"):
            if p["provider"] not in rent_buy:
                rent_buy.append(p["provider"])
    status = "free" if free else ("rent" if rent_buy else "none")
    return {"status": status, "free_channels": free, "subs": subs, "rent_buy": rent_buy}
