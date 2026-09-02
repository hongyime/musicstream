from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest


@pytest.fixture()
def daemon_module(monkeypatch):
    import src.daemon as daemon

    daemon._manual_jobs.clear()
    yield daemon
    daemon._manual_jobs.clear()


@pytest.mark.asyncio
async def test_background_job_reports_completion(daemon_module):
    job = daemon_module._start_background_job("unit", lambda: {"ok": True})
    record = daemon_module._manual_jobs[job["job_id"]]

    await asyncio.wait_for(record["task"], timeout=2)
    view = daemon_module._job_view(record)

    assert job["status"] == "queued"
    assert view["status"] == "completed"
    assert view["result"] == {"ok": True}
    assert view["error"] is None
    assert view["started_at"] is not None
    assert view["completed_at"] is not None


@pytest.mark.asyncio
async def test_background_job_reports_failure(daemon_module):
    def broken():
        raise RuntimeError("boom")

    job = daemon_module._start_background_job("unit", broken)
    record = daemon_module._manual_jobs[job["job_id"]]

    await asyncio.wait_for(record["task"], timeout=2)
    view = daemon_module._job_view(record)

    assert view["status"] == "failed"
    assert "RuntimeError: boom" in view["error"]


@pytest.mark.asyncio
async def test_manual_spotify_sync_returns_job_without_waiting(daemon_module, monkeypatch):
    ran = []

    def fake_sync():
        ran.append(True)

    monkeypatch.setattr(daemon_module.tasks, "spotify_incremental_sync", fake_sync)

    response = await daemon_module.trigger_sync()
    job_id = response.data["job_id"]
    record = daemon_module._manual_jobs[job_id]

    assert response.data["queued"] is True
    assert response.data["status_url"] == f"/api/musicstream/jobs/{job_id}"
    await asyncio.wait_for(record["task"], timeout=2)
    assert ran == [True]


def test_write_health_snapshot_creates_timeline_and_latest(daemon_module, tmp_path, monkeypatch):
    timeline = tmp_path / "health_snapshots.jsonl"
    latest = tmp_path / "health_latest.json"
    monkeypatch.setenv("HEALTH_SNAPSHOT_PATH", str(timeline))
    monkeypatch.setenv("HEALTH_LATEST_PATH", str(latest))

    daemon_module._write_health_snapshot({"status": "ok", "db": True})

    rows = timeline.read_text(encoding="utf-8").splitlines()
    assert len(rows) == 1
    payload = json.loads(rows[0])
    assert payload["status"] == "ok"
    assert payload["db"] is True
    assert payload["manual_jobs"] == {"total": 0, "active": 0}
    assert json.loads(latest.read_text(encoding="utf-8"))["snapshot_at"] == payload["snapshot_at"]


def test_build_deep_health_payload_degrades_on_stale_download_progress(daemon_module, monkeypatch):
    now = datetime.now(timezone.utc)

    class _Query:
        def scalar(self):
            return now

    class _Session:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def execute(self, statement):
            return None

        def query(self, *args):
            return _Query()

    monkeypatch.setattr(daemon_module, "scheduler", SimpleNamespace(running=True))
    monkeypatch.setattr("src.db.get_session", lambda: _Session())
    monkeypatch.setattr(
        daemon_module.tasks,
        "get_download_liveness",
        lambda daemon_uptime_seconds: {
            "pending": 10,
            "downloading": 0,
            "stale_downloading": 0,
            "progress_fresh": False,
        },
    )
    monkeypatch.setattr(
        "src.ingestion.spotify_auth.token_freshness",
        lambda: {"present": True, "hours_left": 1.0},
    )

    payload = daemon_module._build_deep_health_payload()

    assert payload["status"] == "degraded"
    assert "download-progress-stale" in payload["reasons"]
    assert payload["download_liveness"]["progress_fresh"] is False
