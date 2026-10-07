"""Wave 3 blocklist tests (SPEC.md §W3 T13/T14, invariant V7).

Blocked tracks must be inert everywhere:
  - reset_failed_tracks never touches them
  - IntegrityChecker never requeues them (missing/corrupt file stays as-is)
  - auto-block fires after AUTO_BLOCK_THRESHOLD consecutive failed passes
  - block_track / unblock_track task helpers manage the flag
"""

from __future__ import annotations

import pytest

from src.core import config
from src.core.tasks import (
    auto_block_if_exhausted,
    block_track,
    reset_failed_tracks,
    unblock_track,
)
from src.integrity.checker import IntegrityChecker
from src.ingestion.downloader import DownloadOrchestrator
from src.models import DownloadAttempt, Track
from tests.conftest import _make_track


# ── T13: reset-failed honors blocked ─────────────────────────────────────────

def test_reset_failed_skips_blocked(session):
    ok = _make_track(session, "spotify:track:w3ok1", status="failed", attempt_count=3)
    blk = _make_track(
        session,
        "spotify:track:w3blk1",
        status="failed",
        blocked=True,
        blocked_reason="manual test",
    )

    reset_count = reset_failed_tracks(session)
    session.flush()

    assert reset_count == 1, "only the non-blocked failed track should be reset"
    session.refresh(ok)
    session.refresh(blk)
    assert ok.status == "pending"
    assert blk.status == "failed"
    assert blk.blocked is True


# ── T13: integrity requeue honors blocked ────────────────────────────────────

def test_integrity_missing_file_leaves_blocked_track_alone(session):
    missing_path = r"Y:\music\nope\gone.mp3"
    t = _make_track(
        session,
        "spotify:track:w3int1",
        status="downloaded",
        file_path=missing_path,
        file_sha256="a" * 64,
        download_method="ytm",
        blocked=True,
    )

    result = IntegrityChecker().run(session)
    session.flush()

    assert result.missing == 0, "blocked tracks must not be counted/requeued"
    session.refresh(t)
    assert t.status == "downloaded"
    assert t.file_path == missing_path


def test_integrity_corrupt_file_leaves_blocked_track_alone(session, tmp_path):
    f = tmp_path / "song.mp3"
    f.write_bytes(b"corrupted-bytes")
    t = _make_track(
        session,
        "spotify:track:w3int2",
        status="downloaded",
        file_path=str(f),
        file_sha256="b" * 64,
        download_method="ytm",
        blocked=True,
    )

    result = IntegrityChecker().run(session)
    session.flush()

    assert result.corrupt == 0
    session.refresh(t)
    assert t.status == "downloaded"


# ── T14: auto-block after N consecutive complete failed passes ───────────────


def test_auto_block_fires_at_threshold(session, monkeypatch):
    monkeypatch.setattr(config, "AUTO_BLOCK_THRESHOLD", 3)
    t = _make_track(session, "spotify:track:w3ab1", status="pending")
    orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
    for _ in range(3):
        orch._record_pipeline_pass(session, t, "content_miss", "no_candidates")

    hit = auto_block_if_exhausted(session, t)

    session.refresh(t)
    assert hit is True
    assert t.blocked is True
    assert t.blocked_reason and "3" in t.blocked_reason
    assert t.blocked_at is not None
    assert session.query(DownloadAttempt).filter_by(track_id=t.id, method="pipeline_pass").count() == 3


def test_auto_block_does_not_fire_below_threshold(session, monkeypatch):
    monkeypatch.setattr(config, "AUTO_BLOCK_THRESHOLD", 6)
    t = _make_track(session, "spotify:track:w3ab2", status="pending")
    orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
    for _ in range(5):
        orch._record_pipeline_pass(session, t, "content_miss", "no_candidates")

    hit = auto_block_if_exhausted(session, t)

    session.refresh(t)
    assert hit is False
    assert t.blocked is False


def test_auto_block_ignores_successful_attempts(session, monkeypatch):
    """A successful attempt means the track worked before — never auto-block."""
    monkeypatch.setattr(config, "AUTO_BLOCK_THRESHOLD", 2)
    t = _make_track(session, "spotify:track:w3ab3", status="downloaded")
    t.consecutive_failed_passes = 2
    t.last_pipeline_outcome = "success"
    session.flush()

    # downloaded track shouldn't even be considered
    hit = auto_block_if_exhausted(session, t)

    assert hit is False
    session.refresh(t)
    assert t.blocked is False


def test_transient_failure_breaks_streak_and_does_not_auto_block(session, monkeypatch):
    monkeypatch.setattr(config, "AUTO_BLOCK_THRESHOLD", 3)
    t = _make_track(session, "spotify:track:w3transient", status="pending")
    t.consecutive_failed_passes = 2
    t.last_pipeline_outcome = "content_miss"
    orch = DownloadOrchestrator.__new__(DownloadOrchestrator)

    orch._record_pipeline_pass(session, t, "transient_failure", "provider_error")

    assert t.consecutive_failed_passes == 0
    assert auto_block_if_exhausted(session, t) is False
    assert t.blocked is False


# ── Manual block/unblock task helpers (API layer uses these) ─────────────────

def test_block_and_unblock_roundtrip(session):
    t = _make_track(session, "spotify:track:w3rt1", status="failed")
    t.consecutive_failed_passes = 4
    session.flush()

    assert block_track(session, t.id, reason="sounds wrong") is True
    session.refresh(t)
    assert t.blocked is True
    assert t.blocked_reason == "sounds wrong"

    assert unblock_track(session, t.id) is True
    session.refresh(t)
    assert t.blocked is False
    assert t.blocked_reason is None
    assert t.status == "pending", "unblock should give the track a fresh start"
    assert t.consecutive_failed_passes == 0


def test_block_missing_track_returns_false(session):
    assert block_track(session, 999999) is False
    assert unblock_track(session, 999999) is False
