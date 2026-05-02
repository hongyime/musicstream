"""
One-time Spotify PKCE authentication helper.

Run locally (NOT in the container) to generate spotify_token.json:

    SPOTIFY_CLIENT_ID=<your_id> SPOTIFY_TOKEN_CACHE=./spotify_token.json \
        python -m src.ingestion.spotify_auth

Then mount the resulting file into the daemon container:
    volumes:
      - ./spotify_token.json:/app/spotify_token.json

The daemon will use the cached token (and refresh it automatically via spotipy).
"""

from __future__ import annotations

import logging
import os
import sys

import spotipy
from spotipy.cache_handler import CacheFileHandler
from spotipy.oauth2 import SpotifyPKCE

_SCOPES = "playlist-read-private playlist-read-collaborative user-library-read"

logging.basicConfig(level=logging.INFO, format="%(message)s")
logger = logging.getLogger(__name__)


def main() -> None:
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    if not client_id:
        sys.exit("ERROR: SPOTIFY_CLIENT_ID env var is not set.")

    cache_path = os.environ.get("SPOTIFY_TOKEN_CACHE", "./spotify_token.json")
    cache_handler = CacheFileHandler(cache_path=cache_path)

    auth_manager = SpotifyPKCE(
        client_id=client_id,
        redirect_uri="http://127.0.0.1:8888/callback",
        scope=_SCOPES,
        open_browser=True,
        cache_handler=cache_handler,
    )

    sp = spotipy.Spotify(auth_manager=auth_manager)
    user = sp.current_user()
    logger.info("Authenticated as: %s (%s)", user["display_name"], user["id"])
    logger.info("Token cached at: %s", cache_path)
    logger.info(
        "Next step: mount %s into the container at /app/spotify_token.json "
        "(or set SPOTIFY_TOKEN_CACHE in docker-compose.yml)",
        cache_path,
    )


if __name__ == "__main__":
    main()
