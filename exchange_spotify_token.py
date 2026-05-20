import os
import sys
from spotipy.oauth2 import SpotifyOAuth
from spotipy.cache_handler import CacheFileHandler

def exchange_token():
    client_id = "533aed5f09534e1db562d4955a337e82"
    client_secret = "020818b2458442aabea677356176ab54"
    redirect_uri = "http://127.0.0.1:8888/callback"
    scopes = "playlist-read-private user-library-read"
    
    # This matches the redirected URL provided by the user
    redirected_url = "http://127.0.0.1:8888/callback?code=AQAb6VIgS9FQe3x2UEHO5RjsmoEVyOnGKQFlGbpbJlxwslHsjl5R5hbDPh3zo3R_YbkQ2KH2JRkgQ5I3LG3RvN2Uj7OkKJLeXrU7mk84eWGFbN94XT8kireUKyvfE_L23hoYZhjGMe_nlCa6p_CNPXiWVHTo8pgZiv554ZMTJcdij-0gQZxr8fQpy7YF-6s91PznbeZOQ8OrH4tFPtaln44DIs82v4qQLm36lw"
    
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
    
    print(f"Exchanging code for token...")
    code = auth_manager.parse_response_code(redirected_url)
    token = auth_manager.get_access_token(code)
    
    if token:
        print(f"[OK] Token successfully saved to {cache_path}")
    else:
        print(f"[ERROR] Token exchange failed.")

if __name__ == "__main__":
    exchange_token()
