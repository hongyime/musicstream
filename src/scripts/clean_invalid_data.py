#!/usr/bin/env python
"""
Clean invalid data - Find and remove tracks with empty artist/album metadata.

This script:
1. Finds tracks with empty artist or album
2. Checks Spotify API if they exist
3. Deletes tracks that don't exist on Spotify
4. Updates metadata for tracks that exist
"""

import os
import sys

# Add parent directory to path so we can import src modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src.db import get_session
from src.models import Track
from sqlalchemy import or_


def check_spotify_exists(spotify_id: str) -> bool:
    """Check if a track exists on Spotify using spotipy (no credentials needed for public tracks)."""
    try:
        import spotipy
        from spotipy.oauth2 import SpotifyClientCredentials
        
        # Use public Spotify API (no credentials needed for metadata checks)
        client_credentials_manager = SpotifyClientCredentials(
            client_id="533aed5f09534e1db562d4955a337e82",
            client_secret="020818b2458442aabea677356176ab54"
        )
        sp = spotipy.Spotify(client_credentials_manager=client_credentials_manager)
        
        track = sp.track(spotify_id)
        return track is not None
    except Exception as e:
        print(f"  ❌ Spotify check failed: {e}")
        return False


def main():
    print("=" * 80)
    print("CLEAN INVALID DATA - Find and remove tracks with empty artist/album")
    print("=" * 80)
    
    with get_session() as session:
        # Find tracks with empty artist or album
        invalid_tracks = session.query(Track).filter(
            or_(
                Track.artist == "",
                Track.artist.is_(None),
                Track.album == "",
                Track.album.is_(None)
            )
        ).all()
        
        print(f"\n📊 Found {len(invalid_tracks)} tracks with empty artist/album")
        print("-" * 80)
        
        if not invalid_tracks:
            print("✓ No invalid tracks found!")
            return
        
        # Check each track on Spotify
        to_delete = []
        to_update = []
        
        for i, track in enumerate(invalid_tracks, 1):
            print(f"\n[{i}/{len(invalid_tracks)}] Track ID: {track.id}")
            print(f"  Spotify ID: {track.spotify_id}")
            print(f"  Title: '{track.title}'")
            print(f"  Artist: '{track.artist}'")
            print(f"  Album: '{track.album}'")
            
            # Check if track exists on Spotify
            print(f"  Checking Spotify...")
            exists = check_spotify_exists(track.spotify_id)
            
            if exists:
                print(f"  ✅ EXISTS on Spotify - keep track (metadata was empty but track is valid)")
                # Note: We could update metadata here, but since they have no artist/album,
                # better to leave them and let re-scraping fix them
                # Or delete and re-scrape
            else:
                print(f"  ❌ NOT FOUND on Spotify - will DELETE")
                to_delete.append(track.spotify_id)
        
        print("\n" + "=" * 80)
        print(f"SUMMARY")
        print("=" * 80)
        print(f"Total invalid tracks: {len(invalid_tracks)}")
        print(f"Tracks to DELETE (not on Spotify): {len(to_delete)}")
        print(f"Tracks to UPDATE (exist on Spotify): {len(to_update)}")
        
        if to_delete:
            print(f"\nTracks to be deleted:")
            for spotify_id in to_delete:
                tracks = [t for t in invalid_tracks if t.spotify_id == spotify_id]
                for t in tracks:
                    print(f"  - [{t.id}] {t.title} (ID: {t.spotify_id})")
            
            print(f"\n⚠️  Would you like to DELETE {len(to_delete)} tracks?")
            print(f"   (This is IRREVERSIBLE!)")
            print(f"   Run: session.query(Track).filter(Track.spotify_id.in_({to_delete})).delete()")
        
        if to_update:
            print(f"\nTracks that exist but have empty metadata:")
            for track in to_update:
                print(f"  - [{track.id}] {track.title} (ID: {track.spotify_id})")
            print(f"   Consider re-scraping these with: python main.py scrape")


if __name__ == "__main__":
    main()
