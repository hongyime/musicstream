#!/usr/bin/env python3
"""
Test script for refresh artwork endpoint functionality.

Run: python tests/test_refresh_artwork.py

This tests the POST /api/refresh-artwork endpoint with different modes.
"""

import json
import sys
from pathlib import Path

# Add project root to path
project_root = Path(__file__).parent.parent
sys.path.insert(0, str(project_root))

print("🎨 Artwork Refresh Endpoint Test Suite")
print("=" * 60)

# Test data setup
BASE_URL = "http://127.0.0.1:9079"
DAEMON_TOKEN = None  # Set if auth is configured

def get_headers():
    """Set up headers with auth if DAEMON_API_TOKEN is configured."""
    headers = {"Content-Type": "application/json"}
    if DAEMON_TOKEN:
        headers["Authorization"] = f"Bearer {DAEMON_TOKEN}"
    return headers

def test_health_first():
    """Test basic health check before testing artwork endpoints."""
    print("Testing GET /health...")
    try:
        import requests
        response = requests.get(f"{BASE_URL}/health", timeout=10)
        response.raise_for_status()
        data = response.json()
        print(f"✓ Health check OK: {data}")
        return True
    except Exception as e:
        print(f"✗ Health check failed: {e}")
        return False

def test_artwork_report_before():
    """Test GET /api/artwork-report to see state before refresh."""
    print("\nTesting GET /api/artwork-report (before refresh)...")
    try:
        import requests
        response = requests.get(f"{BASE_URL}/api/artwork-report", timeout=30)
        response.raise_for_status()
        data = response.json()
        print(f"✓ Artwork report BEFORE refresh:")
        summary = data.get("summary", {})
        print(f"  Artwork health: {summary.get('artwork_health', 'unknown')}")
        print(f"  DB coverage: {data.get('database', {}).get('coverage_percentage', 'N/A')}%")
        return True
    except Exception as e:
        print(f"✗ Artwork report (before) failed: {e}")
        return False

def test_refresh_artwork_dry_run():
    """Test POST /api/refresh-artwork with dry_run parameter."""
    print("\nTesting POST /api/artwork-refresh (dry run, missing, limit=2)...")
    try:
        import requests
        response = requests.post(
            f"{BASE_URL}/api/artwork-refresh?mode=missing&limit=2&dry_run=1",
            headers=get_headers(),
            timeout=30
        )
        
        if response.status_code == 401:
            print(f"✓ Authentication required - set DAEMON_API_TOKEN")
            return True
            
        response.raise_for_status()
        data = response.json()
        print(f"✓ Refresh artwork (DRY RUN) response:")
        print(json.dumps(data, indent=2))
        
        # Verify structure
        if "summary" in data:
            summary = data["summary"]
            print(f"  Processed: {summary.get('processed', 0)}")
            print(f"  Refreshed: {summary.get('refreshed', 0)}")
            print(f"  Errors: {summary.get('errors', 0)}")
        
        return True
    except Exception as e:
        print(f"✗ Refresh artwork (dry run) failed: {e}")
        return False

def test_refresh_artwork_missing_mode():
    """Test POST /api/refresh-artwork for missing artwork only."""
    print("\nTesting POST /api/artwork-refresh (missing mode, limit=2)...")
    try:
        import requests
        response = requests.post(
            f"{BASE_URL}/api/artwork-refresh?mode=missing&limit=2",
            headers=get_headers(),
            timeout=120  # Allow longer for actual HTTP requests
        )
        
        if response.status_code == 401:
            print(f"✓ Authentication required - set DAEMON_API_TOKEN")
            return True
            
        response.raise_for_status()
        data = response.json()
        print(f"✓ Refresh artwork (MISSING mode) response:")
        print(json.dumps(data, indent=2))
        
        return True
    except Exception as e:
        print(f"✗ Refresh artwork (missing mode) failed: {e}")
        return False

def test_refresh_artwork_all_mode():
    """Test POST /api/artwork-refresh for all tracks (small limit)."""
    print("\nTesting POST /api/artwork-refresh (all mode, dry run, limit=1)...")
    try:
        import requests
        response = requests.post(
            f"{BASE_URL}/api/artwork-refresh?mode=all&limit=1&dry_run=1",
            headers=get_headers(),
            timeout=120
        )
        
        if response.status_code == 401:
            print(f"✓ Authentication required - set DAEMON_TOKEN")
            return True
            
        response.raise_for_status()
        data = response.json()
        print(f"✓ Refresh artwork (ALL mode, dry run) response:")
        print(json.dumps(data, indent=2))
        
        return True
    except Exception as e:
        print(f"✗ Refresh artwork (all mode) failed: {e}")
        return False

def test_invalid_params():
    """Test parameter validation."""
    print("\nTesting parameter validation...")
    try:
        import requests
        
        # Test invalid mode
        response = requests.post(
            f"{BASE_URL}/api/artwork-refresh?mode=invalid&limit=1",
            headers=get_headers(),
            timeout=10
        )
        assert response.status_code == 400, "Should return 400 for invalid mode"
        print(f"✓ Invalid mode correctly rejected (400 expected)")
        
        # Test invalid limit
        response = requests.post(
            f"{BASE_URL}/api/artwork-refresh?mode=missing&limit=0",
            headers=get_headers(),
            timeout=10
        )
        assert response.status_code == 400, "Should return 400 for invalid limit"
        print(f"✓ Invalid limit correctly rejected (400 expected)")
        
        return True
    except Exception as e:
        print(f"✗ Parameter validation test failed: {e}")
        return False

def main():
    """Run all artwork refresh tests."""
    print("=" * 60)
    print("Artwork Refresh Endpoint Test Suite")
    print("=" * 60)
    
    results = []
    
    # Test 1: Health check
    results.append(("Health Check", test_health_first()))
    
    # Test 2: Artwork report before
    results.append(("Artwork Report (Before)", test_artwork_report_before()))
    
    # Test 3: Dry run test
    results.append(("Dry Run", test_refresh_artwork_dry_run()))
    
    # Test 4: Parameter validation
    results.append(("Parameter Validation", test_invalid_params()))
    
    Test 5: Actual missing refresh (optional - only if daemon is running)
    # results.append(("Actual Refresh Missing", test_refresh_artwork_missing_mode()))
    # Test 6: Actual all refresh (optional)
    # results.append(("Actual Refresh All", test_refresh_artwork_all_mode()))
    
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
    
    return 0 if total_passed >= total_tests else 1

if __name__ == "__main__":
    sys.exit(main())
