#!/usr/bin/env python3
"""
Test script for invalid data validation endpoints.

Run: python tests/test_invalid_data_endpoints.py

Expected env variables:
- SPOTIFY_CLIENT_ID: Spotify application client ID
- SPOTIFY_TOKEN_CACHE: Path to cached Spotify token file
"""

import json
import os
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

import requests

BASE_URL = "http://127.0.0.1:9079"
DAEMON_TOKEN = os.environ.get("DAEMON_API_TOKEN")

def get_headers():
    """Set up headers with auth if DAEMON_API_TOKEN is configured."""
    headers = {"Content-Type": "application/json"}
    if DAEMON_TOKEN:
        headers["Authorization"] = f"Bearer {DAEMON_TOKEN}"
    return headers

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

def test_validate_invalid_tracks():
    """Test POST /admin/validate-invalid-tracks."""
    print("\nTesting POST /admin/validate-invalid-tracks...")
    try:
        response = requests.post(
            f"{BASE_URL}/admin/validate-invalid-tracks",
            headers=get_headers(),
            timeout=120  # Allow longer timeout for Spotify API calls
        )
        response.raise_for_status()
        data = response.json()
        print(f"✓ Validation response:")
        print(json.dumps(data, indent=2))
        
        # Verify response structure
        if "summary" in data:
            summary = data["summary"]
            print(f"  Checked: {summary['checked']}")
            print(f"  Updated: {summary['updated']}")
            print(f"  Marked not found: {summary['marked_not_found']}")
            print(f"  Errors: {summary['errors']}")
        
        return True
    except Exception as e:
        print(f"✗ Validation failed: {e}")
        return False

def test_cleanup_invalid_tracks():
    """Test POST /admin/cleanup-invalid-tracks."""
    print("\nTesting POST /admin/cleanup-invalid-tracks...")
    try:
        response = requests.post(
            f"{BASE_URL}/admin/cleanup-invalid-tracks",
            headers=get_headers(),
            timeout=30
        )
        response.raise_for_status()
        data = response.json()
        print(f"✓ Cleanup response:")
        print(json.dumps(data, indent=2))
        
        if "deleted" in data:
            print(f"  Deleted tracks: {data['deleted']}")
        
        return True
    except Exception as e:
        print(f"✗ Cleanup failed: {e}")
        return False

def main():
    """Run all tests."""
    print("=" * 60)
    print("Invalid Data Endpoints Test Suite")
    print("=" * 60)
    
    if not os.environ.get("SPOTIFY_CLIENT_ID"):
        print("⚠ Warning: SPOTIFY_CLIENT_ID not set. Validation may fail.")
    
    results = []
    
    # Test basic health first
    results.append(("Health check", test_health_endpoint()))
    
    # Test validation endpoint
    results.append(("Validate invalid tracks", test_validate_invalid_tracks()))
    
    # Test cleanup endpoint
    results.append(("Cleanup invalid tracks", test_cleanup_invalid_tracks()))
    
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
