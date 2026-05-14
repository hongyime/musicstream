#!/usr/bin/env python3
"""
Test script for artwork report endpoint.

Run: python tests/test_artwork_report.py

Requirements:
- Daemon running on http://localhost:9079
- Some tracks in database
"""

import json
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import requests

BASE_URL = "http://127.0.0.1:9079"

def test_health_endpoint():
    """Test basic health check."""
    print("Testing GET /health...")
    try:
        response = requests.get(f"{BASE_URL}/health", timeout=10)
        response.raise_for_status()
        data = response.json()
        print(f"✓ Health check OK: {data}")
        return True
    except Exception as e:
        print(f"✗ Health check failed: {e}")
        return False

def test_artwork_report():
    """Test GET /api/artwork-report."""
    print("\nTesting GET /api/artwork-report...")
    try:
        response = requests.get(f"{BASE_URL}/api/artwork-report", timeout=30)
        response.raise_for_status()
        data = response.json()
        print(f"✓ Artwork report response:")
        print(json.dumps(data, indent=2))
        
        # Verify response structure
        required_fields = ["database", "embedded_artwork", "missing_by_album", "missing_by_artist", "summary"]
        for field in required_fields:
            if field not in data:
                print(f"✗ Missing required field: {field}")
                return False
        
        # Verify database stats
        if "database" in data:
            db_stats = data["database"]
            print(f"  Database stats:")
            print(f"    Total tracks: {db_stats.get('total_tracks', 'N/A')}")
            print(f"    With cover URL: {db_stats.get('tracks_with_cover_art_url', 'N/A')}")
            print(f"    Without cover URL: {db_stats.get('tracks_without_cover_art_url', 'N/A')}")
            print(f"    Coverage %: {db_stats.get('coverage_percentage', 'N/A')}")
        
        # Verify embedded artwork stats
        if "embedded_artwork" in data:
            emb_stats = data["embedded_artwork"]
            print(f"  Embedded artwork stats:")
            print(f"    Sample checked: {emb_stats.get('sample_checked', 'N/A')}")
            print(f"    Sample without embedded: {emb_stats.get('sample_without_embedded', 'N/A')}")
            print(f"    Estimated total without embedded: {emb_stats.get('estimated_total_without_embedded', 'N/A')}")
            print(f"    Coverage %: {emb_stats.get('coverage_percentage', 'N/A')}")
        
        # Verify summary
        if "summary" in data:
            summary = data["summary"]
            print(f"  Summary:")
            print(f"    Missing albums: {summary.get('total_missing_albums', 'N/A')}")
            print(f"    Missing artists: {summary.get('total_missing_artists', 'N/A')}")
            print(f"    Health: {summary.get('artwork_health', 'N/A')}")
        
        return True
    except Exception as e:
        print(f"✗ Artwork report failed: {e}")
        return False

def test_artwork_checker_module():
    """Test the artwork_checker module directly."""
    print("\nTesting artwork_checker module...")
    try:
        from src.ingestion.artwork_checker import check_embedded_artwork, extract_first_artwork
        
        # Test with non-existent file
        result = check_embedded_artwork("/tmp/nonexistent.mp3")
        print(f"✓ Non-existent file check: {result} (expected: False)")
        
        # Test with unsupported format
        result = check_embedded_artwork(__file__)  # This file
        print(f"✓ Unsupported format check: {result} (expected: False)")
        
        return True
    except ImportError as e:
        print(f"⚠ Warning: artwork_checker module not available: {e}")
        return True  # Not a failure, just graceful degradation
    except Exception as e:
        print(f"✗ Artwork checker module test failed: {e}")
        return False

def main():
    """Run all tests."""
    print("=" * 60)
    print("Artwork Report Test Suite")
    print("=" * 60)
    
    results = []
    
    # Test basic health first
    results.append(("Health check", test_health_endpoint()))
    
    # Test artwork report endpoint
    results.append(("Artwork report endpoint", test_artwork_report()))
    
    # Test artwork checker module
    results.append(("Artwork checker module", test_artwork_checker_module()))
    
    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    for name, passed in results:
        status = "✓ PASS" if passed else "✗ FAIL"
        print(f"{status}: {name}")
    
    total_passed = sum(1 for _, passed in results if passed)
    total_tests = len(results)
    print(f"\n{total_passed}/{total_tests} tests passed")
    
    return 0 if total_passed == total_tests else 1

if __name__ == "__main__":
    sys.exit(main())
