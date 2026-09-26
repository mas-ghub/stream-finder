"""App settings — everything optional so the app runs with zero config and just
gets better as you add a TMDB key."""
from __future__ import annotations

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_prefix="SF_", extra="ignore")

    # TMDB — free keys from https://www.themoviedb.org/settings/api
    # Two keys, both free (that page shows "API Read Access Token" + "API Key"):
    #   tmdb_api_key  = the v3 "API Key"  -> powers "where to watch" (streaming providers).
    #   tmdb_v4_token = the v4 "Read Access Token" -> powers details/credits/trailers for
    #                   every title (v3 keys 404 on the detail endpoints).
    tmdb_enabled: bool = True
    tmdb_api_key: str = ""
    tmdb_v4_token: str = ""

    # TVMaze — free, no key. Backbone for shows.
    tvmaze_enabled: bool = True

    # Rotten Tomatoes enrichment — scraped, no key.
    rt_enabled: bool = True

    # Optional: a free Serper.dev key makes RT show-URL resolution reliable.
    # Movies work without it; add it to also get ratings for TV shows.
    serper_api_key: str = ""

    # Behaviour
    http_timeout: float = 12.0
    cache_ttl: int = 3600  # seconds, search cache

    # Hard ceiling on how many results the search endpoint may return, no matter
    # what the client asks for (a safety cap on the "max results" setting).
    max_results: int = 1000


settings = Settings()
