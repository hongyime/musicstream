"""
src/ingestion/spotify_auth.py -- One-time Spotify OAuth helper.

Run this locally (on a machine with a browser) to generate spotify_token.json.
The daemon uses the saved token without ever needing a browser.

Usage:
    python -m src.ingestion.spotify_auth

Or via setup.bat (Step 4) which calls this automatically.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Auto-load .env if present so this script works standalone
try:
    from dotenv import load_dotenv
    load_dotenv(Path(__file__).parent.parent.parent / ".env")
except ImportError:
    pass
from spotipy.cache_handler import CacheFileHandler
from spotipy.oauth2 import SpotifyPKCE

_SCOPES = (
    "playlist-read-private "
    "playlist-read-collaborative "
    "user-library-read "
    "user-follow-read "
    "user-read-recently-played"
)


def main() -> None:
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    if not client_id:
        print("[ERROR] SPOTIFY_CLIENT_ID not set. Check your .env file.")
        sys.exit(1)

    # Default to local path when running outside Docker
    cache_path = os.environ.get("SPOTIFY_TOKEN_CACHE", "./spotify_token.json")
    # If the env var points to the Docker container path, override to local
    if cache_path == "/app/spotify_token.json":
        cache_path = "./spotify_token.json"

    print(f"[INFO] Authenticating with Spotify (client_id={client_id[:8]}...)")
    print(f"[INFO] Token will be saved to: {cache_path}")
    print("[INFO] A browser window will open. Log in and click Allow.")
    print()

    cache_handler = CacheFileHandler(cache_path=cache_path)
    auth_manager = SpotifyPKCE(
        client_id=client_id,
        redirect_uri="http://127.0.0.1:8888/callback",
        scope=_SCOPES,
        open_browser=True,
        cache_handler=cache_handler,
    )

    # Trigger the OAuth flow -- opens browser, waits for redirect
    # get_access_token with no args triggers the full PKCE flow
    auth_manager.get_access_token(code=None)
    # Verify token was cached
    if not auth_manager.get_cached_token():
        print("[ERROR] Failed to obtain access token.")
        sys.exit(1)

    # Verify it works
    import spotipy
    sp = spotipy.Spotify(auth_manager=auth_manager)
    user = sp.current_user()
    display_name = user.get("display_name") or user.get("id", "unknown")

    print(f"[OK]   Authenticated as: {display_name}")
    print(f"[OK]   Token saved to: {cache_path}")
    print()
    print("Next steps:")
    print("  - If running setup.bat: it will continue automatically")
    print("  - If on a separate dev machine: copy spotify_token.json to your")
    print("    production machine's musicstream folder, then run startup.bat")


# ── Wave 3 token freshness probe (§W3 T18/V13) ──────────────────────────────

def token_freshness(cache_path: str | None = None) -> dict:
    """Read the cached Spotify token and report how stale it is.

    Returns {'present': bool, 'hours_left': float|None}. Never raises —
    a missing/corrupt cache simply reports present=False.
    """
    import json
    from datetime import datetime, timezone

    path = cache_path or os.environ.get("SPOTIFY_TOKEN_CACHE", "./spotify_token.json")
    if path == "/app/spotify_token.json":
        path = "./spotify_token.json"
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        expires_at = float(data.get("expires_at", 0))
    except Exception:
        return {"present": False, "hours_left": None}

    hours_left = (expires_at - datetime.now(timezone.utc).timestamp()) / 3600.0
    return {"present": True, "hours_left": round(hours_left, 2)}


def probe_token(cache_path: str | None = None, refresher=None, max_age_hours=None) -> dict:
    """Early-warning probe (§W3 T18/V13).

    Semantics note: Spotify ACCESS tokens live ~1h by design, so comparing
    hours_left against TOKEN_WARN_HOURS would flag a healthy setup forever.
    The real health signal is whether a silent REFRESH succeeds:

      degraded = cache unreadable/missing, OR the token sat inside the warn
                 window and the refresh attempt FAILED.

    A successful self-healing refresh is healthy (degraded=False) even
    though the new access token again has only ~1h to live. With no
    refresher supplied (display-only callers), degraded simply means the
    cache is missing/unreadable — staleness alone is normal.
    """
    from src.core import config as _config

    warn_hours = max_age_hours if max_age_hours is not None else _config.TOKEN_WARN_HOURS
    info = token_freshness(cache_path=cache_path)

    stale = (not info["present"]) or (info["hours_left"] < warn_hours)
    refreshed = False
    if stale and refresher is not None:
        try:
            refreshed = bool(refresher())
        except Exception:
            refreshed = False
        if refreshed:
            info = token_freshness(cache_path=cache_path)

    if refresher is None:
        # Display-only: only a missing/unreadable cache needs human action.
        degraded = not info["present"]
    else:
        degraded = (not info["present"]) or (stale and not refreshed)

    return {**info, "degraded": degraded, "refreshed": refreshed}

if __name__ == "__main__":
    main()
