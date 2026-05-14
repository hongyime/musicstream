import os
import sys

try:
    from spotdl import Spotdl
    print("✓ Spotdl import succeeded")
    
    # Check environment variables
    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
    
    print(f"SPOTIFY_CLIENT_ID: {'SET' if client_id else 'NOT SET'}")
    print(f"SPOTIFY_CLIENT_SECRET: {'SET' if client_secret else 'NOT SET'}")
    if client_id:
        print(f"Client ID (first 8 chars): {client_id[:8]}")
    if client_secret:
        print(f"Client Secret length: {len(client_secret)} chars")
    
except ImportError as e:
    print(f"✗ Spotdl import failed: {e}")
    sys.exit(1)
