"""Shared data models for the one-stop stream finder."""
from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class Ratings:
    """Normalized rating block. TMDB vote (0-10) + RT critic/audience (%)."""

    tmdb_vote: float | None = None
    tmdb_count: int | None = None
    rt_tomatometer: int | None = None  # 0-100 critic score
    rt_audience: int | None = None  # 0-100 audience score
    rt_fresh: bool | None = None
    imdb: float | None = None  # 0-10 if we ever manage it


@dataclass
class Result:
    title: str
    kind: str  # "movie" | "show"
    year: int | None = None
    genres: list[str] = field(default_factory=list)  # canonical
    poster_url: str | None = None
    backdrop_url: str | None = None
    overview: str | None = None
    directors: list[str] = field(default_factory=list)
    cast: list[str] = field(default_factory=list)
    cast_full: list[dict] = field(default_factory=list)  # [{name, person_id, profile}]
    ratings: Ratings | None = None
    trailer_url: str | None = None  # youtube watch/embed url
    external_url: str | None = None  # where to watch / detail page
    runtime_minutes: int | None = None
    imdb_id: str | None = None
    tmdb_id: int | None = None
    language: str | None = None  # original language code (e.g. 'en', 'ko')
    platforms: list[dict] = field(default_factory=list)  # [{provider, service, type}]
    sources: list[str] = field(default_factory=list)  # source keys that matched

    def to_dict(self) -> dict:
        return {
            "title": self.title,
            "kind": self.kind,
            "year": self.year,
            "genres": self.genres,
            "poster_url": self.poster_url,
            "backdrop_url": self.backdrop_url,
            "overview": self.overview,
            "directors": self.directors,
            "cast": self.cast,
            "cast_full": self.cast_full,
            "ratings": self.ratings.__dict__ if self.ratings else None,
            "trailer_url": self.trailer_url,
            "external_url": self.external_url,
            "runtime_minutes": self.runtime_minutes,
            "imdb_id": self.imdb_id,
            "tmdb_id": self.tmdb_id,
            "language": self.language,
            "platforms": self.platforms,
            "sources": self.sources,
        }

    # convenience composite score (0-10) for sorting
    def score(self) -> float | None:
        if self.ratings:
            vals = [self.ratings.tmdb_vote]
            if self.ratings.rt_tomatometer:
                vals.append(self.ratings.rt_tomatometer / 10)
            vals = [v for v in vals if v is not None]
            if vals:
                return round(sum(vals) / len(vals), 2)
        return None
