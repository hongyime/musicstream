#!/usr/bin/env python3
"""
Trigger a manual download run to test the fixes.
"""

import sys
import os

# Add the src directory to the path
sys.path.insert(0, '/app/src')

from src.db import SessionMaker
from src.ingestion.downloader import DownloadOrchestrator

def test_downloader():
    """Test the downloader with a few tracks"""
    print("Starting download test...")
    
    session = SessionMaker()
    orchestrator = DownloadOrchestrator()
    
    try:
        # Get up to 3 pending tracks
        from src.models import Track, TrackStatus
        pending_tracks = (
            session.query(Track)
            .filter(Track.status == TrackStatus.PENDING.value)
            .limit(3)
            .all()
        )
        
        if not pending_tracks:
            print("No pending tracks to test with")
            return
        
        print(f"Found {len(pending_tracks)} pending tracks")
        for track in pending_tracks:
            print(f"  - {track.title} by {track.artist}")
        
        # Run download for these tracks
        downloaded, failed = orchestrator.download_pending(session)
        
        print(f"\nResults: Downloaded={downloaded}, Failed={failed}")
        
        # Check for any errors in recent logs
        print("\nRecent download activity:")
        
    finally:
        session.close()

if __name__ == "__main__":
    test_downloader()
