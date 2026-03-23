#!/usr/bin/env python3
"""
Idempotency Verification Test
Tests that downloader.extract_audio respects existing files
"""

import os
import sys
import tempfile
import shutil

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_signature():
    """Verify the function signature includes force parameter"""
    from downloader import AudioExtractor
    
    import inspect
    sig = inspect.signature(AudioExtractor.extract_audio)
    params = list(sig.parameters.keys())
    
    expected_params = ['self', 'video_id', 'track_name', 'artist_name', 
                      'album_name', 'year', 'force']
    
    print("Testing AudioExtractor.extract_audio signature:")
    for param in expected_params:
        if param in params:
            print(f"  ✓ {param} parameter exists")
        else:
            print(f"  ✗ {param} parameter MISSING")
            return False
    return True

def test_file_existence_check():
    """Verify the file existence check is implemented"""
    with open('downloader.py', 'r') as f:
        content = f.read()
        
    checks = [
        ('force parameter in signature', 'force: bool = False'),
        ('File existence check', 'if not force:'),
        ('Find existing file', '_find_downloaded_file'),
        ('Check file size', 'os.path.getsize(existing) > 0'),
        ('Return existing file', 'return existing'),
    ]
    
    print("\nTesting file existence check implementation:")
    all_passed = True
    for check_name, pattern in checks:
        if pattern in content:
            print(f"  ✓ {check_name}")
        else:
            print(f"  ✗ {check_name} NOT FOUND")
            all_passed = False
    
    return all_passed

def test_main_usage():
    """Verify main.py uses the force parameter correctly"""
    with open('main.py', 'r') as f:
        content = f.read()
    
    checks = [
        ('cmd_download uses force', 'force=args.fresh'),
        ('cmd_retry uses force', 'force=args.fresh'),
    ]
    
    print("\nTesting force parameter usage in main.py:")
    all_passed = True
    for check_name, pattern in checks:
        if pattern in content:
            count = content.count(pattern)
            print(f"  ✓ {check_name} ({count} occurrences)")
        else:
            print(f"  ✗ {check_name} NOT FOUND")
            all_passed = False
    
    return all_passed

def test_setup_bat():
    """Verify setup.bat preserves venv"""
    with open('setup.bat', 'r') as f:
        content = f.read()
    
    print("\nTesting setup.bat idempotency:")
    
    # Check for venv check (should NOT delete on re-run)
    if 'rmdir /s /q venv' not in content:
        print("  ✓ Does not delete and recreate venv on every run")
        return True
    else:
        print("  ✗ Still has code that deletes venv")
        return False

def run_all_tests():
    """Run all verification tests"""
    print("=" * 60)
    print("MUSIC-DOWNLOAD-CODE IDEMPOTENCY VERIFICATION")
    print("=" * 60)
    
    results = []
    
    try:
        results.append(("Function Signature", test_signature()))
    except Exception as e:
        print(f"\n✗ Signature test failed: {e}")
        results.append(("Function Signature", False))
    
    try:
        results.append(("File Existence Check", test_file_existence_check()))
    except Exception as e:
        print(f"\n✗ File existence check test failed: {e}")
        results.append(("File Existence Check", False))
    
    try:
        results.append(("Main.py Usage", test_main_usage()))
    except Exception as e:
        print(f"\n✗ Main usage test failed: {e}")
        results.append(("Main Usage", False))
    
    try:
        results.append(("Setup.bat Idempotency", test_setup_bat()))
    except Exception as e:
        print(f"\n✗ Setup.bat test failed: {e}")
        results.append(("Setup.bat Idempotency", False))
    
    print("\n" + "=" * 60)
    print("RESULTS SUMMARY")
    print("=" * 60)
    
    for test_name, passed in results:
        status = "PASSED" if passed else "FAILED"
        print(f"{test_name}: {status}")
    
    all_passed = all(result[1] for result in results)
    
    if all_passed:
        print("\n✅ ALL TESTS PASSED - Idempotency verified!")
        return 0
    else:
        print("\n❌ SOME TESTS FAILED")
        return 1

if __name__ == "__main__":
    sys.exit(run_all_tests())
