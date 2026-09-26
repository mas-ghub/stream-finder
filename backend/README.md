# Backend for the Stream Finder what-to-watch app.
# Deployed as a web service on Render (free tier) — see /deploy/render/ for the blueprint.
#
# Required environment variables (Render → service → Environment):
#   SF_TMDB_API_KEY   (v3 "API Key")         -> where-to-watch (streaming providers)
#   SF_TMDB_V4_TOKEN  (v4 "Read Access Token") -> details, cast, trailers
#   SF_SERPER_API_KEY (optional)              -> Rotten Tomatoes ratings for TV shows
#
# Render injects $PORT — the app already binds it (see app/main.py __main__).
