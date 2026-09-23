import asyncio
import subprocess
from collections.abc import AsyncIterator, Iterator
from contextlib import contextmanager
from pathlib import Path
from threading import Event
from unittest.mock import Mock

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import Session, sessionmaker

from src.models import Base, Track

TEST_TOKEN = "test_token_123"
pytestmark = pytest.mark.asyncio

@pytest.fixture()
def forbid_external_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    forbidden = Mock(side_effect=AssertionError(
        "Endpoint fixture must not launch a daemon or connect to a live database"
    ))
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr("src.db.wait_for_db", forbidden)
    monkeypatch.setattr("src.db.run_migrations", forbidden)


@pytest_asyncio.fixture()
async def api_client(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, forbid_external_startup: None,
) -> AsyncIterator[AsyncClient]:
    import src.daemon as daemon
    import src.db as db

    engine = create_engine(f"sqlite:///{tmp_path / 'endpoints.db'}")
    try:
        Base.metadata.create_all(engine)
        monkeypatch.setattr(db, "_engine", engine)
        monkeypatch.setattr(db, "_session_factory", sessionmaker(bind=engine))
        monkeypatch.setattr(daemon, "DAEMON_API_TOKEN", TEST_TOKEN)
        # ASGITransport exercises routes without production DB/scheduler lifespan.
        async with AsyncClient(
            transport=ASGITransport(app=daemon.app), base_url="http://test",
        ) as client:
            yield client
    finally:
        engine.dispose()

def get_headers() -> dict[str, str]:
    return {"Authorization": f"Bearer {TEST_TOKEN}", "Content-Type": "application/json"}

@pytest.mark.integration
async def test_validate_invalid_tracks(api_client: AsyncClient) -> None:
    resp = await api_client.post(
        "/admin/validate-invalid-tracks", headers=get_headers(),
    )
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    assert resp.json() == {
        "summary": {"checked": 0, "updated": 0, "marked_not_found": 0, "errors": 0}
    }

@pytest.mark.integration
async def test_cleanup_invalid_tracks(api_client: AsyncClient) -> None:
    resp = await api_client.post("/admin/cleanup-invalid-tracks", headers=get_headers())
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    assert resp.json() == {"deleted": 0}

@pytest.mark.integration
async def test_artwork_report(api_client: AsyncClient) -> None:
    resp = await api_client.get("/api/artwork-report", headers=get_headers())
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    assert resp.json()["summary"] == {"artwork_health": "unknown"}

@pytest.mark.integration
async def test_refresh_artwork(api_client: AsyncClient) -> None:
    resp = await api_client.post("/api/artwork-refresh", headers=get_headers())
    assert resp.status_code == 200, f"Expected 200, got {resp.status_code}: {resp.text}"
    assert resp.json() == {"summary": {"processed": 0, "refreshed": 0, "errors": 0}}


async def test_endpoint_fixture_avoids_external_startup(
    api_client: AsyncClient,
) -> None:
    response = await api_client.get("/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": True}


@pytest.mark.parametrize("params", [{"mode": "invalid"}, {"limit": 0}])
async def test_refresh_artwork_rejects_invalid_input(
    api_client: AsyncClient, params: dict[str, str | int],
) -> None:
    response = await api_client.post(
        "/api/artwork-refresh", params=params, headers=get_headers(),
    )
    assert response.status_code == 400


@pytest.mark.parametrize(
    "authorization, status", [(None, 401), ("Bearer wrong", 403), ("invalid", 401)],
)
async def test_mutation_requires_auth(
    api_client: AsyncClient, authorization: str | None, status: int,
) -> None:
    headers = {"Authorization": authorization} if authorization else {}
    response = await api_client.post("/admin/cleanup-invalid-tracks", headers=headers)
    assert response.status_code == status


@pytest.mark.parametrize("route", [
    ("GET", "/health"),
    ("GET", "/api/musicstream/stats"),
    ("GET", "/api/musicstream/burn-rate"),
    ("GET", "/api/musicstream/tracks"),
    ("GET", "/api/musicstream/metrics"),
    ("GET", "/api/musicstream/library"),
    ("POST", "/api/musicstream/tracks/reset-failed"),
    ("POST", "/api/musicstream/tracks/1/block"),
    ("POST", "/api/musicstream/tracks/1/unblock"),
    ("POST", "/api/musicstream/upgrade-pass"),
    ("POST", "/api/musicstream/discover-weekly"),
])
async def test_slow_health_probe_does_not_block_other_routes(
    api_client: AsyncClient, monkeypatch: pytest.MonkeyPatch, route: tuple[str, str],
) -> None:
    from src.db import get_session

    entered = Event()
    release = Event()

    @contextmanager
    def slow_session() -> Iterator[Session]:
        if not entered.is_set():
            entered.set()
            release.wait(5)
        with get_session() as session:
            yield session

    monkeypatch.setattr("src.db.get_session", slow_session)
    monkeypatch.setattr(
        "src.discovery.discover_weekly.DiscoverWeekly",
        Mock(return_value=Mock(run=Mock(return_value={}))),
    )
    method, path = route
    blocked_request = asyncio.create_task(
        api_client.request(method, path, headers=get_headers()),
    )
    try:
        assert await asyncio.to_thread(entered.wait, 10)
        response = await api_client.get("/health")
        assert response.status_code == 200
        assert not blocked_request.done(), f"{path} blocked health on the ASGI loop"
    finally:
        release.set()
        await blocked_request


async def test_stats_counts_mixed_statuses_in_one_query(
    api_client: AsyncClient,
) -> None:
    import src.db as db

    statuses = [
        "downloaded", "downloaded", "pending", "failed",
        "failed_validation", "timed_out", "downloading", "not_found",
    ]
    with db.get_session() as session:
        session.add_all([
            Track(spotify_uri=f"test:stats:{index}", title="Test", artist="Test",
                  status=status)
            for index, status in enumerate(statuses)
        ])
    assert db._engine is not None
    query_observer = Mock()
    event.listen(db._engine, "before_cursor_execute", query_observer)
    try:
        response = await api_client.get("/api/musicstream/stats")
    finally:
        event.remove(db._engine, "before_cursor_execute", query_observer)

    assert response.status_code == 200
    assert response.json()["data"] == {
        "total_tracks": 8, "downloaded": 2, "pending": 1,
        "failed": 3, "active": 1, "progress_pct": 25.0,
    }
    assert query_observer.call_count == 1, "Dashboard stats must use one DB round trip"


async def test_stats_handles_empty_library(api_client: AsyncClient) -> None:
    response = await api_client.get("/api/musicstream/stats")
    assert response.status_code == 200
    assert response.json()["data"] == {
        "total_tracks": 0, "downloaded": 0, "pending": 0,
        "failed": 0, "active": 0, "progress_pct": 0.0,
    }
