"""Tests for src/core/tasks.py reset/requeue helpers."""
from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import Track, TrackStatus  # noqa: E402
from src.core import tasks  # noqa: E402
from src.core.tasks import reset_failed_tracks, reset_orphaned_downloads  # noqa: E402


def _track(session, uri, status, attempt_count):
    t = Track(
        spotify_uri=uri,
        spotify_id=uri.split(":")[-1],
        title="t",
        artist="a",
        album="al",
        status=status,
        attempt_count=attempt_count,
        last_attempt_at=datetime.now(timezone.utc),
        claimed_at=datetime.now(timezone.utc),
        heartbeat_at=datetime.now(timezone.utc),
        claim_owner="worker:test",
        daemon_run_id=123,
    )
    session.add(t)
    session.flush()
    return t


class TestResetFailedTracks:
    """reset_failed_tracks must requeue failed-family tracks AND clear
    attempt_count, or _should_give_up re-fails them on the first tier miss."""

    def test_clears_attempt_count_on_failed(self, session):
        t = _track(session, "spotify:track:rf1", "failed", attempt_count=25)
        n = reset_failed_tracks(session)
        session.expire_all()
        rt = session.get(Track, t.id)
        assert n == 1
        assert rt.status == TrackStatus.PENDING.value
        assert (rt.attempt_count or 0) == 0
        assert rt.last_attempt_at is None
        assert rt.claimed_at is None
        assert rt.heartbeat_at is None
        assert rt.claim_owner is None
        assert rt.daemon_run_id is None

    def test_covers_failed_validation_and_timed_out(self, session):
        a = _track(session, "spotify:track:rf2", "failed_validation", attempt_count=30)
        b = _track(session, "spotify:track:rf3", "timed_out", attempt_count=40)
        n = reset_failed_tracks(session)
        session.expire_all()
        assert n == 2
        ra = session.get(Track, a.id)
        rb = session.get(Track, b.id)
        assert ra.status == TrackStatus.PENDING.value
        assert (ra.attempt_count or 0) == 0
        assert ra.claim_owner is None
        assert (rb.attempt_count or 0) == 0
        assert rb.claim_owner is None

    def test_leaves_pending_and_downloaded_untouched(self, session):
        p = _track(session, "spotify:track:rf4", "pending", attempt_count=3)
        d = _track(session, "spotify:track:rf5", "downloaded", attempt_count=7)
        reset_failed_tracks(session)
        session.expire_all()
        # neither status is in the reset filter, so attempt_count is preserved
        assert session.get(Track, p.id).attempt_count == 3
        assert session.get(Track, d.id).attempt_count == 7
        assert session.get(Track, d.id).status == "downloaded"


class TestResetOrphanedDownloads:
    def test_resets_stale_heartbeat_and_clears_claim(self, session):
        now = datetime.now(timezone.utc)
        stale = _track(session, "spotify:track:orphan1", "downloading", attempt_count=1)
        stale.heartbeat_at = now - timedelta(minutes=45)
        stale.claim_owner = "worker:stale"

        fresh = _track(session, "spotify:track:orphan2", "downloading", attempt_count=1)
        fresh.heartbeat_at = now - timedelta(minutes=5)
        fresh.claim_owner = "worker:fresh"
        session.flush()

        @contextmanager
        def fake_get_session():
            yield session

        with patch("src.db.get_session", fake_get_session):
            n = reset_orphaned_downloads(all_rows=False, stale_after_minutes=30)

        session.expire_all()
        stale = session.get(Track, stale.id)
        fresh = session.get(Track, fresh.id)
        assert n == 1
        assert stale.status == TrackStatus.PENDING.value
        assert stale.heartbeat_at is None
        assert stale.claim_owner is None
        assert fresh.status == TrackStatus.DOWNLOADING.value
        assert fresh.claim_owner == "worker:fresh"

    def test_resets_old_rows_without_heartbeat_by_updated_at(self, session):
        old = _track(session, "spotify:track:orphan3", "downloading", attempt_count=1)
        old.heartbeat_at = None
        old.updated_at = datetime.now(timezone.utc) - timedelta(minutes=45)
        old.claim_owner = "worker:old"
        session.flush()

        @contextmanager
        def fake_get_session():
            yield session

        with patch("src.db.get_session", fake_get_session):
            n = reset_orphaned_downloads(all_rows=False, stale_after_minutes=30)

        session.expire_all()
        old = session.get(Track, old.id)
        assert n == 1
        assert old.status == TrackStatus.PENDING.value
        assert old.claim_owner is None

    def test_all_rows_resets_fresh_active_claims_on_boot(self, session):
        active = _track(session, "spotify:track:orphan4", "downloading", attempt_count=1)
        active.heartbeat_at = datetime.now(timezone.utc)
        active.claim_owner = "worker:active"
        session.flush()

        @contextmanager
        def fake_get_session():
            yield session

        with patch("src.db.get_session", fake_get_session):
            n = reset_orphaned_downloads(all_rows=True)

        session.expire_all()
        active = session.get(Track, active.id)
        assert n == 1
        assert active.status == TrackStatus.PENDING.value
        assert active.heartbeat_at is None
        assert active.claim_owner is None


class TestDownloadLiveness:
    def _bind_session(self, session):
        @contextmanager
        def fake_get_session():
            yield session

        return patch("src.db.get_session", fake_get_session)

    def test_degrades_when_pending_and_no_success_after_threshold(self, session):
        from src.models import DownloadAttempt

        _track(session, "spotify:track:live1", "pending", attempt_count=0)
        attempt = DownloadAttempt(
            track_id=_track(session, "spotify:track:live2", "downloaded", attempt_count=0).id,
            attempted_at=datetime.now(timezone.utc) - timedelta(hours=8),
            method="unit",
            success=True,
        )
        session.add(attempt)
        session.flush()

        with self._bind_session(session), patch.object(tasks, "DISABLE_DOWNLOADS", False):
            info = tasks.get_download_liveness(
                max_stale_hours=6,
                startup_grace_seconds=60,
                daemon_uptime_seconds=120,
            )

        assert info["pending"] == 1
        assert info["progress_fresh"] is False
        assert info["last_success_age_seconds"] >= 6 * 3600

    def test_ok_during_startup_grace(self, session):
        _track(session, "spotify:track:live3", "pending", attempt_count=0)

        with self._bind_session(session), patch.object(tasks, "DISABLE_DOWNLOADS", False):
            info = tasks.get_download_liveness(
                max_stale_hours=6,
                startup_grace_seconds=300,
                daemon_uptime_seconds=30,
            )

        assert info["pending"] == 1
        assert info["past_startup_grace"] is False
        assert info["progress_fresh"] is True

    def test_ok_when_no_pending_backlog(self, session):
        _track(session, "spotify:track:live4", "downloaded", attempt_count=0)

        with self._bind_session(session), patch.object(tasks, "DISABLE_DOWNLOADS", False):
            info = tasks.get_download_liveness(
                max_stale_hours=6,
                startup_grace_seconds=60,
                daemon_uptime_seconds=120,
            )

        assert info["pending"] == 0
        assert info["progress_fresh"] is True

    def test_reports_stale_downloading_count(self, session):
        stale = _track(session, "spotify:track:live5", "downloading", attempt_count=1)
        stale.heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=45)
        fresh = _track(session, "spotify:track:live6", "downloading", attempt_count=1)
        fresh.heartbeat_at = datetime.now(timezone.utc) - timedelta(minutes=5)
        session.flush()

        with self._bind_session(session), patch.object(tasks, "DISABLE_DOWNLOADS", False):
            info = tasks.get_download_liveness(stale_after_minutes=30)

        assert info["downloading"] == 2
        assert info["stale_downloading"] == 1


def test_requeue_stale_downloads_uses_configured_threshold(monkeypatch):
    calls = []

    def fake_reset_orphaned_downloads(*, all_rows=False, stale_after_minutes=30):
        calls.append((all_rows, stale_after_minutes))
        return 3

    monkeypatch.setenv("STALE_DOWNLOAD_MINUTES", "12")
    monkeypatch.setattr(tasks, "reset_orphaned_downloads", fake_reset_orphaned_downloads)

    assert tasks.requeue_stale_downloads() == 3
    assert calls == [(False, 12)]
