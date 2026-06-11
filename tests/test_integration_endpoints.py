import os
import subprocess
import sys
import time
import requests
import pytest
from typing import Generator

# Ensure we're hitting the daemon on a test port to avoid conflicting with prod
TEST_PORT = 9089
BASE_URL = f"http://127.0.0.1:{TEST_PORT}"
TEST_TOKEN = "test_token_123"

@pytest.fixture(scope="module")
def daemon_process() -> Generator[subprocess.Popen, None, None]:
    # Set environment variables for the daemon
    env = os.environ.copy()
    env["DAEMON_API_TOKEN"] = TEST_TOKEN
    env["PORT"] = str(TEST_PORT)
    env["PYTHONPATH"] = str(os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))
    
    # Start the daemon
    process = subprocess.Popen(
        [sys.executable, "-m", "uvicorn", "src.daemon:app", "--port", str(TEST_PORT)],
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE
    )
    
    # Wait for the daemon to start up
    max_retries = 30
    for i in range(max_retries):
        try:
            resp = requests.get(f"{BASE_URL}/health", timeout=2)
            if resp.status_code in (200, 503): # It might be 503 if DB is not ready, but it's responding
                break
        except (requests.ConnectionError, requests.exceptions.ReadTimeout):
            time.sleep(1)
    else:
        process.terminate()
        stdout, stderr = process.communicate()
        raise RuntimeError(f"Daemon failed to start in time.\nSTDOUT:\n{stdout.decode()}\nSTDERR:\n{stderr.decode()}")
        
    yield process
    
    # Teardown
    process.terminate()
    process.wait(timeout=5)

def get_headers():
    return {"Authorization": f"Bearer {TEST_TOKEN}", "Content-Type": "application/json"}

@pytest.mark.integration
def test_validate_invalid_tracks(daemon_process):
    resp = requests.post(f"{BASE_URL}/admin/validate-invalid-tracks", headers=get_headers())
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

@pytest.mark.integration
def test_cleanup_invalid_tracks(daemon_process):
    resp = requests.post(f"{BASE_URL}/admin/cleanup-invalid-tracks", headers=get_headers())
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

@pytest.mark.integration
def test_artwork_report(daemon_process):
    resp = requests.get(f"{BASE_URL}/api/artwork-report", headers=get_headers())
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"

@pytest.mark.integration
def test_refresh_artwork(daemon_process):
    resp = requests.post(f"{BASE_URL}/api/artwork-refresh", headers=get_headers())
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
