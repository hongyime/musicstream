"""One-shot Spotify OAuth refresh — headless.

Usage: python refresh_spotify_token.py

Runs a local HTTP listener on 127.0.0.1:8888 that catches the OAuth callback.
Prints the auth URL for you to paste into your browser. Once you click
"Authorize", Spotify redirects to the local listener, the code is exchanged
for a token, and spotify_token.json is written atomically. Then the listener
exits.

Docker daemon picks up the new token file automatically via bind-mount on
next scraper cycle (every 15 min) — no restart needed.

Kill with Ctrl+C if you change your mind; nothing is written on abort.
"""
from __future__ import annotations

import json
import os
import sys
import time
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from urllib.parse import urlparse, parse_qs

try:
    from spotipy.oauth2 import SpotifyOAuth
except ImportError:
    print("spotipy not installed. Run: pip install spotipy", file=sys.stderr)
    sys.exit(1)

# Same values as spotify_cli_login.py — kept in-source for zero-config UX.
CLIENT_ID = "533aed5f09534e1db562d4955a337e82"
CLIENT_SECRET = "020818b2458442aabea677356176ab54"
REDIRECT_URI = "http://127.0.0.1:8888/callback"
SCOPES = (
    "playlist-read-private playlist-read-collaborative "
    "user-library-read user-follow-read user-read-recently-played"
)
CACHE_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "spotify_token.json")

_result: dict = {}


class _CallbackHandler(BaseHTTPRequestHandler):
    def do_GET(self):  # noqa: N802 — stdlib callback name
        parsed = urlparse(self.path)
        params = parse_qs(parsed.query)
        code = params.get("code", [None])[0]
        err = params.get("error", [None])[0]
        if code:
            _result["code"] = code
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.end_headers()
            self.wfile.write(
                b"<html><body style='font-family:sans-serif;padding:2em'>"
                b"<h1>OK</h1><p>Token captured. You can close this tab.</p>"
                b"</body></html>"
            )
        elif err:
            _result["error"] = err
            self.send_response(400)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.end_headers()
            self.wfile.write(f"OAuth error: {err}\n".encode())
        else:
            self.send_response(404)
            self.end_headers()

    def log_message(self, *_args, **_kw):  # silence noisy default logging
        pass


def main() -> int:
    oauth = SpotifyOAuth(
        client_id=CLIENT_ID,
        client_secret=CLIENT_SECRET,
        redirect_uri=REDIRECT_URI,
        scope=SCOPES,
        cache_path=CACHE_PATH,
        open_browser=False,
    )
    auth_url = oauth.get_authorize_url()

    print("=" * 68)
    print("SPOTIFY OAUTH REFRESH — HEADLESS")
    print("=" * 68)
    print()
    print("1. Open this URL in a browser (any device on this LAN):")
    print()
    print(f"   {auth_url}")
    print()
    print("2. Click Authorize. You'll be redirected to a 'success' page here.")
    print("3. Token gets written to:", CACHE_PATH)
    print()

    server = HTTPServer(("127.0.0.1", 8888), _CallbackHandler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()

    print("Listening on 127.0.0.1:8888 — waiting for callback… (Ctrl+C to abort)")
    try:
        deadline = time.time() + 600  # 10 min ceiling
        while not _result and time.time() < deadline:
            time.sleep(0.5)
    except KeyboardInterrupt:
        print("\nAborted. Nothing written.")
        server.shutdown()
        return 1

    server.shutdown()

    if "error" in _result:
        print(f"\nOAuth error: {_result['error']}")
        return 1
    if "code" not in _result:
        print("\nTimeout — no callback received within 10 minutes.")
        return 1

    code = _result["code"]
    token_info = oauth.get_access_token(code, as_dict=True, check_cache=False)
    if not token_info:
        print("\nToken exchange failed.")
        return 1

    # spotipy already wrote to CACHE_PATH via its cache handler.
    print(f"\nSUCCESS. Token cached at {CACHE_PATH}")
    print(f"  expires_at = {token_info.get('expires_at')}")
    print(f"  scope      = {token_info.get('scope')}")
    print("\nDaemon picks up the new token on next scraper cycle (~15 min max).")
    return 0


if __name__ == "__main__":
    sys.exit(main())
