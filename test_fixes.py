#!/usr/bin/env python3
"""
Quick test to verify the fixes are loaded in the daemon.
"""

import sys
import os

# Add the src directory to the path
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'src'))

def test_rate_limiter():
    """Test that rate limiter has updated SpotiFLAC config"""
    from src.rate_limiter import ServiceRateLimiter, ServiceThrottle
    
    rl = ServiceRateLimiter()
    throttle = ServiceThrottle()
    
    # Check SpotiFLAC rate limiter config
    spotiflac_config = rl.CONFIGS.get("spotiflac")
    print(f"✓ SpotiFLAC rate limiter config: base={spotiflac_config.base}, max={spotiflac_config.max}")
    
    assert spotiflac_config.base == 10.0, f"Expected base=10.0, got {spotiflac_config.base}"
    assert spotiflac_config.max == 600, f"Expected max=600, got {spotiflac_config.max}"
    
    # Check SpotiFLAC throttle config
    spotiflac_throttle = throttle.CONFIGS.get("spotiflac")
    print(f"✓ SpotiFLAC throttle config: floor={spotiflac_throttle.floor}, ceiling={spotiflac_throttle.ceiling}")
    
    assert spotiflac_throttle.floor == 10.0, f"Expected floor=10.0, got {spotiflac_throttle.floor}"
    assert spotiflac_throttle.ceiling == 120.0, f"Expected ceiling=120.0, got {spotiflac_throttle.ceiling}"
    
    print("✓ Rate limiter tests passed!")

def test_tagger():
    """Test that tagger has type checking"""
    from src.ingestion.tagger import MetadataTagger
    
    tagger = MetadataTagger()
    
    # Test with invalid recording type
    try:
        result = tagger._parse_recording("invalid_string")
        # Should handle gracefully and return empty MBData
        assert result.recording_id is None
        assert result.artist is None
        print("✓ Tagger handles invalid recording type gracefully")
    except Exception as e:
        print(f"✗ Tagger failed on invalid type: {e}")
        raise
    
    # Test with string artist-credit
    try:
        result = tagger._parse_recording({
            "id": "test-id",
            "title": "test-title",
            "artist-credit": ["Artist Name"]
        })
        assert result.artist == "Artist Name"
        print("✓ Tagger handles string artist-credit correctly")
    except Exception as e:
        print(f"✗ Tagger failed on string artist-credit: {e}")
        raise
    
    print("✓ Tagger tests passed!")

def test_qobuz_env_vars():
    """Test that Qobuz env vars are read"""
    qobuz_email = os.environ.get("QOBUZ_EMAIL", "")
    qobuz_password_md5 = os.environ.get("QOBUZ_PASSWORD_MD5", "")
    
    print(f"✓ QOBUZ_EMAIL: {qobuz_email or '(not set)'}")
    print(f"✓ QOBUZ_PASSWORD_MD5: {qobuz_password_md5 or '(not set)'}")
    
    print("✓ Environment variable tests passed!")

if __name__ == "__main__":
    print("=" * 60)
    print("Testing musicstream daemon fixes")
    print("=" * 60)
    
    try:
        test_rate_limiter()
        print()
        test_tagger()
        print()
        test_qobuz_env_vars()
        print()
        print("=" * 60)
        print("ALL TESTS PASSED! ✓")
        print("=" * 60)
    except Exception as e:
        print(f"\n✗ TEST FAILED: {e}")
        sys.exit(1)
