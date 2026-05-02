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
from spotipy.oauth2 import SpotifyPKCE

_SCOPES = (
    "playlist-read-private "
    "playlist-read-collaborative "
    "user-library-read "
    "user-follow-read "
    "user-read-playback-history"
)


def main() -> None:
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    if not client_id:
        print("[ERROR] SPOTIFY_CLIENT_ID not set. Check your .env file.")
        sys.exit(1)

    cache_path = os.environ.get("SPOTIFY_TOKEN_CACHE", "./spotify_token.json")

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
    token = auth_manager.get_access_token(as_dict=False)
    if not token:
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


if __name__ == "__main__":
    main()
