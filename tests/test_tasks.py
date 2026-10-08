"""Tests for src/core/tasks.py reset/requeue helpers."""
from __future__ import annotations

import sys
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from src.models import DownloadAttempt, DownloadAttemptAggregate, Track, TrackStatus  # noqa: E402
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
        t.content_failure_passes = 6
        t.transient_failure_passes = 4
        t.consecutive_failed_passes = 6
        t.next_retry_at = datetime.now(timezone.utc) + timedelta(hours=1)
        t.last_pipeline_outcome = "content_miss"
        t.last_pipeline_error = "no_candidates"
        t.last_pipeline_pass_at = datetime.now(timezone.utc)
        n = reset_failed_tracks(session)
        session.expire_all()
        rt = session.get(Track, t.id)
        assert n == 1
        assert rt.status == TrackStatus.PENDING.value
        assert (rt.attempt_count or 0) == 0
        assert rt.last_attempt_at is None
        assert rt.content_failure_passes == 0
        assert rt.transient_failure_passes == 0
        assert rt.consecutive_failed_passes == 0
        assert rt.next_retry_at is None
        assert rt.last_pipeline_outcome is None
        assert rt.last_pipeline_error is None
        assert rt.last_pipeline_pass_at is None
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


class TestOrphanFileInventory:
    def test_reports_unowned_audio_and_missing_db_files_without_mutation(
        self, session, tmp_path, monkeypatch,
    ):
        media = tmp_path / "media"
        media.mkdir()
        owned = media / "owned.mp3"
        referenced_pending = media / "pending.flac"
        orphan = media / "unowned.mp3"
        quarantined = media / "old.flac.orphan-12345"
        ignored = media / "notes.txt"
        for path in (owned, referenced_pending, orphan, quarantined, ignored):
            path.write_bytes(b"fixture")

        downloaded = _track(session, "spotify:track:inv1", "downloaded", 0)
        downloaded.file_path = str(owned)
        missing = _track(session, "spotify:track:inv2", "downloaded", 0)
        missing.file_path = str(media / "missing.m4a")
        pending = _track(session, "spotify:track:inv3", "pending", 0)
        pending.file_path = str(referenced_pending)
        session.flush()

        @contextmanager
        def fake_get_session():
            yield session

        monkeypatch.setattr("src.db.get_session", fake_get_session)
        inventory = tasks.orphan_file_inventory(media, sample_limit=10)

        assert inventory["orphan_file_count"] == 2
        assert set(inventory["orphan_file_samples"]) == {str(orphan), str(quarantined)}
        assert inventory["missing_file_count"] == 1
        assert inventory["missing_file_samples"] == [{
            "track_id": missing.id,
            "status": "downloaded",
            "file_path": str(media / "missing.m4a"),
        }]
        assert inventory["scan_error_count"] == 0
        assert all(path.is_file() for path in (owned, referenced_pending, orphan, quarantined, ignored))

    def test_refuses_inventory_when_media_root_is_unavailable(self, tmp_path):
        with pytest.raises(FileNotFoundError, match="Media root is unavailable"):
            tasks.orphan_file_inventory(tmp_path / "missing")


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


def test_download_attempt_pruning_preserves_aggregates_and_pass_state(session, monkeypatch):
    track = _track(session, "spotify:track:attempt_retention", "pending", attempt_count=12)
    track.consecutive_failed_passes = 4
    now = datetime.now(timezone.utc)
    session.add_all([
        DownloadAttempt(
            track_id=track.id, attempted_at=now - timedelta(days=40),
            method="tier2_ytdlp_ytm", success=True,
        ),
        DownloadAttempt(
            track_id=track.id, attempted_at=now - timedelta(days=40),
            method="tier2_ytdlp_ytm", success=False,
        ),
        DownloadAttempt(
            track_id=track.id, attempted_at=now - timedelta(days=40),
            method=None, success=False,
        ),
        DownloadAttempt(
            track_id=track.id, attempted_at=now - timedelta(days=2),
            method="tier2_ytdlp_ytm", success=False,
        ),
    ])
    session.flush()

    @contextmanager
    def fake_get_session():
        yield session

    monkeypatch.setattr("src.db.get_session", fake_get_session)
    monkeypatch.setattr("src.core.config.DOWNLOAD_ATTEMPT_RETENTION_DAYS", 30)

    assert tasks.prune_download_attempts() == 3
    assert session.query(DownloadAttempt).filter_by(track_id=track.id).count() == 1
    aggregates = {
        (row.method, row.success): row.total_count
        for row in session.query(DownloadAttemptAggregate).all()
    }
    assert aggregates == {
        ("tier2_ytdlp_ytm", True): 1,
        ("tier2_ytdlp_ytm", False): 1,
        ("unknown", False): 1,
    }
    # Auto-block streak state is on tracks and stays independent of pruned rows.
    assert session.get(Track, track.id).consecutive_failed_passes == 4
