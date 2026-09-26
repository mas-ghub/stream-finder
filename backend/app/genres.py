"""Central genre vocabulary + normalization.

Maps the many platform-specific genre spellings (TVMaze, TMDB, Netflix,
Sky, etc.) onto one canonical set, and exposes a friendly "mood" list for the UI.
"""
from __future__ import annotations

import re

# Canonical genres -> the set of aliases we will fold into them (lowercased).
CANONICAL: dict[str, list[str]] = {
    "Action": ["action", "action/adventure", "adventure", "adventure/action", "action adventure", "action & adventure"],
    "Comedy": ["comedy", "sitcom", "sit-com", "sit com", "sitcoms", "comedy/sitcom", "romantic comedy", "romance/comedy", "comedy-romance"],
    "Drama": ["drama"],
    "Horror": ["horror", "thriller/horror"],
    "Thriller": ["thriller", "crime/thriller", "mystery/thriller", "suspense", "mystery"],
    "Crime": ["crime", "true crime", "crime drama", "police", "crime comedy"],
    "Sci-Fi": ["science fiction", "sci-fi", "scifi", "science-fiction", "futuristic", "space", "fantasy/sci-fi"],
    "Fantasy": ["fantasy", "supernatural", "fantasy/sci-fi"],
    "Romance": ["romance"],
    "Documentary": ["documentary", "docs", "documentaries", "non-fiction", "nature", "wildlife"],
    "Kids & Family": ["kids", "family", "children", "animation", "animated", "anime", "cartoons", "kids/family"],
    "Animation": ["animation", "animated", "anime", "cartoons"],
    "Music": ["music", "musical", "concerts"],
    "Reality": ["reality", "reality tv", "competition", "game show", "talk show", "lifestyle"],
    "War": ["war", "historical"],
    "History": ["history", "historical", "period"],
    "Sports": ["sports", "sport", "documentary/sports"],
    "Western": ["western"],
    "Mystery": ["mystery"],
    "Adventure": ["adventure"],
    "Biography": ["biography", "biographical", "true story"],
    "News": ["news", "current affairs"],
    "Stand-up": ["stand-up", "stand up", "comedy/stand-up"],
}

_ALIASES: dict[str, str] = {}
for canon, aliases in CANONICAL.items():
    for a in aliases:
        _ALIASES[a] = canon
_ALIASES.update({c.lower(): c for c in CANONICAL})


def normalize_genre(g: str) -> str | None:
    """Map any platform genre string onto a canonical genre, or None."""
    if not g:
        return None
    key = g.strip().lower()
    if key in _ALIASES:
        return _ALIASES[key]
    # substring fallbacks for odd compound genres
    for canon in CANONICAL:
        if canon.lower() in key:
            return canon
    return None


def normalize_genres(raw: list[str]) -> list[str]:
    out: list[str] = []
    for g in raw:
        c = normalize_genre(g)
        if c and c not in out:
            out.append(c)
    return out


# The moods a user might pick in the UI, mapped to canonical genres.
MOODS: list[dict[str, object]] = [
    {"mood": "Horror", "genres": ["Horror"]},
    {"mood": "Thriller", "genres": ["Thriller", "Crime", "Mystery"]},
    {"mood": "Sitcom / Comedy", "genres": ["Comedy", "Sitcom", "Stand-up"]},
    {"mood": "Action", "genres": ["Action", "Adventure"]},
    {"mood": "Sci-Fi / Fantasy", "genres": ["Sci-Fi", "Fantasy"]},
    {"mood": "Drama", "genres": ["Drama"]},
    {"mood": "Romance", "genres": ["Romance"]},
    {"mood": "Kids & Family", "genres": ["Kids & Family", "Animation"]},
    {"mood": "Documentary", "genres": ["Documentary"]},
    {"mood": "Crime / True Crime", "genres": ["Crime", "Mystery"]},
    {"mood": "Reality TV", "genres": ["Reality"]},
    {"mood": "Sport", "genres": ["Sports"]},
]


def genres_for_mood(mood: str) -> list[str]:
    for m in MOODS:
        if m["mood"] == mood:
            return list(m["genres"])  # type: ignore[arg-type]
    return [mood]
