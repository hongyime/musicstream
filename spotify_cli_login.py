import os
import sys
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import CacheFileHandler

def run_auth():
    client_id = "533aed5f09534e1db562d4955a337e82"
    client_secret = "020818b2458442aabea677356176ab54"
    # Use the 8888 callback which is also in your portal
    redirect_uri = "http://127.0.0.1:8888/callback"
    # Bare minimum scopes to reduce error chance
    scopes = "playlist-read-private user-library-read"
    
    cache_path = "./spotify_token.json"
    cache_handler = CacheFileHandler(cache_path=cache_path)
    
    auth_manager = SpotifyOAuth(
        client_id=client_id,
        client_secret=client_secret,
        redirect_uri=redirect_uri,
        scope=scopes,
        cache_handler=cache_handler,
        open_browser=False
    )
    
    auth_url = auth_manager.get_authorize_url()
    print("\n" + "="*60)
    print("SPOTIFY CLI LOGIN")
    print("="*60)
    print("\n1. Paste this URL into your browser:")
    print(f"\n{auth_url}\n")
    print("2. Log in and click 'Authorize'.")
    print("3. You will be redirected to a page that doesn't load (127.0.0.1:8888).")
    print("4. COPY the entire URL of that 'dead' page and PASTE it here.")
    print("\nWaiting for redirect URL...")
    
    redirected_url = input("\nPaste URL here: ").strip()
    
    if redirected_url:
        code = auth_manager.parse_response_code(redirected_url)
        token = auth_manager.get_access_token(code)
        if token:
            print("\n[OK] Success! Token saved to ./spotify_token.json")
            print("I will now copy this to the Docker container for you.")
        else:
            print("\n[ERROR] Failed to exchange code for token.")
    else:
        print("\n[CANCELLED] No URL provided.")

if __name__ == "__main__":
    run_auth()
