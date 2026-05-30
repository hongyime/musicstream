"""
Tests for musicstream/integrity/checker.py

Correctness properties verified:
  P9: After integrity check, no downloaded track has a missing or corrupt file
  - Missing file → status reset to pending, file_path/sha256 cleared
  - Corrupt file (hash mismatch) → status reset to pending
  - OK file → status unchanged, last_checked_at updated
  - last_checked_at always updated regardless of outcome
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models import Base, Track, TrackStatus
from src.integrity.checker import IntegrityChecker, IntegrityResult


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    sess = Session()
    yield sess
    sess.rollback()
    sess.close()


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _make_downloaded_track(session, uri, file_path, sha256, **kwargs):
    t = Track(
        spotify_uri=uri,
        title=kwargs.get("title", "Song"),
        artist=kwargs.get("artist", "Artist"),
        status=TrackStatus.DOWNLOADED.value,
        file_path=file_path,
        file_sha256=sha256,
        cover_art_source="none",
    )
    session.add(t)
    session.flush()
    return t


# ── IntegrityResult dataclass ─────────────────────────────────────────────────

class TestIntegrityResult:
    def test_default_counts_are_zero(self):
        r = IntegrityResult()
        assert r.missing == 0
        assert r.corrupt == 0
        assert r.ok == 0
        assert r.total_checked == 0


# ── IntegrityChecker.run() ────────────────────────────────────────────────────

class TestIntegrityCheckerRun:
    def test_ok_file_increments_ok_count(self, session):
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
            f.write(b"audio data")
            path = f.name
        sha = _sha256(b"audio data")
        try:
            _make_downloaded_track(session, "spotify:track:ok001", path, sha)
            checker = IntegrityChecker()
            result = checker.run(session)
            assert result.ok >= 1
        finally:
            os.unlink(path)

    def test_missing_file_resets_to_pending(self, session):
        """P9: missing file → status=pending, file_path=None, file_sha256=None."""
        track = _make_downloaded_track(
            session,
            "spotify:track:missing001",
            "/nonexistent/path/song.mp3",
            "deadbeef" * 8,
        )
        checker = IntegrityChecker()
        result = checker.run(session)

        assert result.missing >= 1
        assert track.status == TrackStatus.PENDING.value
        assert track.file_path is None
        assert track.file_sha256 is None

    def test_corrupt_file_resets_to_pending(self, session):
        """P9: hash mismatch → status=pending."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".flac") as f:
            f.write(b"original content")
            path = f.name
        wrong_sha = _sha256(b"different content")
        try:
            track = _make_downloaded_track(
                session,
                "spotify:track:corrupt001",
                path,
                wrong_sha,
            )
            checker = IntegrityChecker()
            result = checker.run(session)

            assert result.corrupt >= 1
            assert track.status == TrackStatus.PENDING.value
            # Corrupt branch intentionally PRESERVES file_path + file_sha256 as
            # forensic evidence (checker.py: "keep original for forensics"),
            # unlike the missing-file branch which clears them. The track still
            # re-enters the queue via status=pending and re-downloads next cycle.
            assert track.file_path is not None
            assert track.file_sha256 is not None
        finally:
            if os.path.exists(path):
                os.unlink(path)

    def test_last_checked_at_always_updated(self, session):
        """last_checked_at must be updated even for OK files."""
        with tempfile.NamedTemporaryFile(delete=False, suffix=".mp3") as f:
            f.write(b"good audio")
            path = f.name
        sha = _sha256(b"good audio")
        try:
            track = _make_downloaded_track(
                session,
                "spotify:track:checked001",
                path,
                sha,
            )
            assert track.last_checked_at is None
            checker = IntegrityChecker()
            checker.run(session)
            assert track.last_checked_at is not None
        finally:
            os.unlink(path)

    def test_pending_tracks_not_checked(self, session):
        """Only downloaded tracks with file_path are checked."""
        pending = Track(
            spotify_uri="spotify:track:pending_skip",
            title="Pending",
            artist="Artist",
            status=TrackStatus.PENDING.value,
            cover_art_source="none",
        )
        session.add(pending)
        session.flush()

        checker = IntegrityChecker()
        result = checker.run(session)
        # Pending track should not appear in total_checked
        # (we can't assert exact count due to other tests, but pending track
        #  should still be pending after the check)
        assert pending.status == TrackStatus.PENDING.value

    def test_returns_integrity_result_type(self, session):
        checker = IntegrityChecker()
        result = checker.run(session)
        assert isinstance(result, IntegrityResult)

    def test_total_checked_equals_sum_of_outcomes(self, session):
        checker = IntegrityChecker()
        result = checker.run(session)
        assert result.total_checked == result.ok + result.missing + result.corrupt

    def test_no_downloaded_tracks_returns_zero_counts(self, engine):
        """Fresh session with no downloaded tracks → all zeros."""
        from sqlalchemy.orm import sessionmaker as sm
        Session = sm(bind=engine, expire_on_commit=False)
        sess = Session()
        # Delete all tracks to get a clean slate
        sess.query(Track).delete()
        sess.flush()
        checker = IntegrityChecker()
        result = checker.run(sess)
        assert result.total_checked == 0
        assert result.ok == 0
        assert result.missing == 0
        assert result.corrupt == 0
        sess.rollback()
        sess.close()
