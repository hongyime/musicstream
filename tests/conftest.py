"""
pytest conftest.py - Shared test fixtures and configuration.

Provides centralized database fixtures with proper isolation.
"""
import os
import pytest
from pathlib import Path

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, Session
from sqlalchemy.pool import StaticPool

import sys
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models import Base, Track, Source, DownloadAttempt

# ── Collection exclusions (P2-10) ──────────────────────────────────────
# These are MANUAL integration scripts (run via `python tests/<name>.py`
# against a live daemon), not pytest unit tests — one imports a removed symbol,
# one is a print-based smoke script. Exclude them so automated collection of
# the real unit tests is not blocked.
collect_ignore = ["test_download.py", "test_refresh_artwork.py", "test_invalid_data_endpoints.py"]

# ── Hypothesis stability ──────────────────────────────────────────────────────
# Disable the per-example deadline so property tests don't flake on a loaded
# CI/dev box (timing varies under load; the assertions don't). Standard CI
# practice; does not weaken any test. Guarded in case hypothesis is absent.
try:
    from hypothesis import settings as _hyp_settings, HealthCheck as _HC
    _hyp_settings.register_profile(
        "ms", deadline=None, suppress_health_check=[_HC.too_slow]
    )
    _hyp_settings.load_profile("ms")
except Exception:  # pragma: no cover - hypothesis optional
    pass

# ── Test Database Configuration ───────────────────────────────────────────────

# Use in-memory SQLite for fast, isolated tests
TEST_DATABASE_URL = "sqlite:///:memory:"


@pytest.fixture(scope="session")
def engine():
    """
    Create a test database engine for the entire test session.
    Uses SQLite in-memory database for isolation and speed.
    """
    engine = create_engine(
        TEST_DATABASE_URL,
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    
    # Create all tables
    Base.metadata.create_all(bind=engine)
    
    yield engine
    
    # Clean up after all tests
    Base.metadata.drop_all(bind=engine)


@pytest.fixture(scope="function")
def session(engine) -> Session:
    """
    Create a fresh database session for each test.
    Ensures rollback between tests for proper isolation.
    """
    connection = engine.connect()
    transaction = connection.begin()
    
    # Create a session bound to this transaction
    SessionLocal = sessionmaker(bind=connection)
    test_session = SessionLocal()
    
    yield test_session
    
    # Rollback transaction and close session after each test
    test_session.close()
    transaction.rollback()
    connection.close()


# ── Test Data Factory Functions ──────────────────────────────────────────────────

def _make_track(session: Session, spotify_uri: str, **kwargs) -> Track:
    """Factory function to create test track objects."""
    defaults = {
        "spotify_uri": spotify_uri,
        "spotify_id": spotify_uri.split(":")[-1] if ":" in spotify_uri else spotify_uri,
        "title": f"Test Track {spotify_uri}",
        "artist": "Test Artist",
        "album": "Test Album",
        "status": "pending"
    }
    defaults.update(kwargs)
    
    track = Track(**defaults)
    session.add(track)
    session.flush()
    return track


def _make_source(session: Session, spotify_id: str, source_type: str = "playlist", **kwargs) -> Source:
    """Factory function to create test source objects."""
    defaults = {
        "spotify_id": spotify_id,
        "name": f"Test Playlist {spotify_id}",
        "source_type": source_type,
    }
    defaults.update(kwargs)
    
    source = Source(**defaults)
    session.add(source)
    session.flush()
    return source


def _make_download_attempt(session: Session, track_id: int, **kwargs) -> DownloadAttempt:
    """Factory function to create test download attempt objects."""
    from datetime import datetime, timezone
    
    defaults = {
        "track_id": track_id,
        "attempted_at": datetime.now(timezone.utc),
        "method": "test_method",
        "success": True,
        "error": None,
    }
    defaults.update(kwargs)
    
    attempt = DownloadAttempt(**defaults)
    session.add(attempt)
    session.flush()
    return attempt


# ── Shared Test Utilities ────────────────────────────────────────────────────

@pytest.fixture(scope="function")
def sample_track_data():
    """
    Provide sample track data for tests.
    Returns a dict with expected track fields.
    """
    return {
        "spotify_uri": "spotify:track:test123",
        "spotify_id": "test123",
        "title": "Test Song",
        "artist": "Test Artist",
        "album": "Test Album",
        "year": "2024",
        "status": "pending",
    }


@pytest.fixture(scope="function")
def sample_source_data():
    """
    Provide sample source data for tests.
    Returns a dict with expected source fields.
    """
    return {
        "spotify_id": "playlist123",
        "name": "Test Playlist",
        "source_type": "playlist",
    }


@pytest.fixture(scope="function")
def temp_db_path(tmp_path):
    """
    Create a temporary file path for tests that need file-based databases.
    """
    return tmp_path / "test.db"


# ── Environment Variable Management ────────────────────────────────────────────

@pytest.fixture(autouse=True)
def reset_environment_variables():
    """
    Reset environment variables before each test to ensure test isolation.
    Override MAX_CONCURRENT_WORKERS, DISABLE_DOWNLOADS, etc.
    """
    # Save original values
    original_env = {}
    
    # Store and reset important environment variables
    variables_to_reset = [
        "MAX_CONCURRENT_WORKERS",
        "DISABLE_DOWNLOADS",
        "DAEMON_API_TOKEN",
        "SPOTIFY_CLIENT_ID",
        "SPOTIFY_TOKEN_CACHE",
        "LISTENBRAINZ_USERNAME",
        "LISTENBRAINZ_TOKEN",
    ]
    
    for var in variables_to_reset:
        if var in os.environ:
            original_env[var] = os.environ[var]
            del os.environ[var]
    
    yield
    
    # Restore original values after test
    for var, value in original_env.items():
        os.environ[var] = value


@pytest.fixture(scope="function")  
def mock_spotify_token(tmp_path):
    """
    Create a mock Spotify token file for testing.
    Returns the file path.
    """
    token_data = {
        "access_token": "mock_access_token",
        "token_type": "Bearer",
        "expires_in": 3600,
        "refresh_token": "mock_refresh_token",
        "scope": "playlist-read-private user-library-read",
        "expires_at": 9999999999,  # Far future
    }
    
    import json
    
    token_file = tmp_path / "spotify_token.json"
    with open(token_file, "w") as f:
        json.dump(token_data, f)
    
    return str(token_file)


# ── Pytest Configuration ─────────────────────────────────────────────────────────

@pytest.fixture(scope="session")
def worker_id(request):
    """
    Get worker ID for parallel test execution.
    Useful for creating unique test data when tests run in parallel.
    """
    if hasattr(request.config, "workerinput"):
        return request.config.workerinput.get("workerid", "master")
    return "master"


def pytest_configure(config):
    """
    Pytest configuration hook.
    Register custom markers and configure test behavior.
    """
    config.addinivalue_line(
        "markers",
        "slow: marks tests as slow (deselect with '-m \"not slow\"')"
    )
    config.addinivalue_line(
        "markers",
        "integration: marks tests as integration tests (requires outside services)"
    )
    config.addinivalue_line(
        "markers", 
        "unit: marks tests as unit tests (no external dependencies)"
    )
    config.addinivalue_line(
        "markers",
        "requires_spotify: marks tests that require Spotify authentication"
    )
    config.addinivalue_line(
        "markers",
        "requires_db: marks tests that require database"
    )
