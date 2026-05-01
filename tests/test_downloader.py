"""
Tests for musicstream/ingestion/downloader.py

Covers:
  - _should_give_up(): threshold logic (≥9 failed attempts)
  - _record_attempt(): writes DownloadAttempt row
  - _resolve_method_label(): correct label per tier
  - _build_mp3_opts(): correct yt-dlp options
  - download_track(): single failure never raises (P8), status transitions
  - MAX_CONCURRENT constant
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

# Mock yt_dlp, spotipy, ytmusicapi, spotdl before importing downloader
for _mod in ("yt_dlp", "spotipy", "spotipy.oauth2", "ytmusicapi", "spotdl"):
    sys.modules.setdefault(_mod, MagicMock())

from models import Base, DownloadAttempt, Track, TrackStatus
from ingestion.downloader import DownloadOrchestrator, _GIVE_UP_THRESHOLD


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


def _utcnow():
    return datetime.now(timezone.utc)


def _make_track(session, uri="spotify:track:dl001"):
    t = Track(
        spotify_uri=uri,
        title="Download Test",
        artist="Artist",
        status=TrackStatus.PENDING.value,
        duration_ms=180000,
        cover_art_source="none",
    )
    session.add(t)
    session.flush()
    return t


def _add_failed_attempts(session, track_id, count):
    for i in range(count):
        a = DownloadAttempt(
            track_id=track_id,
            attempted_at=_utcnow(),
            method=f"tier{(i % 5) + 1}",
            error="simulated failure",
            success=False,
        )
        session.add(a)
    session.flush()


# ── Constants ─────────────────────────────────────────────────────────────────

class TestConstants:
    def test_max_concurrent_is_4(self):
        assert DownloadOrchestrator.MAX_CONCURRENT == 4

    def test_give_up_threshold_is_9(self):
        assert _GIVE_UP_THRESHOLD == 9


# ── _should_give_up ───────────────────────────────────────────────────────────

class TestShouldGiveUp:
    def test_below_threshold_returns_false(self, session):
        track = _make_track(session, "spotify:track:giveup_below")
        _add_failed_attempts(session, track.id, 8)
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        assert orch._should_give_up(session, track.id) is False

    def test_at_threshold_returns_true(self, session):
        track = _make_track(session, "spotify:track:giveup_at")
        _add_failed_attempts(session, track.id, 9)
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        assert orch._should_give_up(session, track.id) is True

    def test_above_threshold_returns_true(self, session):
        track = _make_track(session, "spotify:track:giveup_above")
        _add_failed_attempts(session, track.id, 12)
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        assert orch._should_give_up(session, track.id) is True

    def test_zero_attempts_returns_false(self, session):
        track = _make_track(session, "spotify:track:giveup_zero")
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        assert orch._should_give_up(session, track.id) is False

    def test_successful_attempts_not_counted(self, session):
        track = _make_track(session, "spotify:track:giveup_success")
        # Add 9 successful attempts — should NOT trigger give-up
        for _ in range(9):
            a = DownloadAttempt(
                track_id=track.id,
                attempted_at=_utcnow(),
                method="tier1_spotiflac",
                success=True,
            )
            session.add(a)
        session.flush()
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        assert orch._should_give_up(session, track.id) is False


# ── _record_attempt ───────────────────────────────────────────────────────────

class TestRecordAttempt:
    def test_writes_attempt_row(self, session):
        track = _make_track(session, "spotify:track:record001")
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._record_attempt(session, track.id, "tier2_ytdlp_ytm", "network error", False)
        attempt = (
            session.query(DownloadAttempt)
            .filter_by(track_id=track.id)
            .first()
        )
        assert attempt is not None
        assert attempt.method == "tier2_ytdlp_ytm"
        assert attempt.error == "network error"
        assert attempt.success is False

    def test_success_attempt_recorded(self, session):
        track = _make_track(session, "spotify:track:record_success")
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._record_attempt(session, track.id, "tier1_spotiflac", None, True)
        attempt = (
            session.query(DownloadAttempt)
            .filter_by(track_id=track.id)
            .first()
        )
        assert attempt.success is True
        assert attempt.error is None


# ── _resolve_method_label ─────────────────────────────────────────────────────

class TestResolveMethodLabel:
    @pytest.mark.parametrize("tier,path,expected", [
        ("tier1_spotiflac", "/tmp/abc_qobuz.flac",    "spotiflac_qobuz"),
        ("tier1_spotiflac", "/tmp/abc_tidal.flac",    "spotiflac_tidal"),
        ("tier1_spotiflac", "/tmp/abc_amazon.flac",   "spotiflac_amazon"),
        ("tier1_spotiflac", "/tmp/abc_deezer.flac",   "spotiflac_deezer"),
        ("tier1_spotiflac", "/tmp/abc_youtube.flac",  "spotiflac_youtube"),
        ("tier2_ytdlp_ytm",     "/tmp/x.mp3",  "ytdlp_ytm"),
        ("tier3_spotdl",        "/tmp/x.mp3",  "spotdl"),
        ("tier4_ytdlp_youtube", "/tmp/x.mp3",  "ytdlp_yt"),
        ("tier5_ytdlp_soundcloud", "/tmp/x.mp3", "ytdlp_soundcloud"),
    ])
    def test_label(self, tier, path, expected):
        assert DownloadOrchestrator._resolve_method_label(tier, path) == expected


# ── _build_mp3_opts ───────────────────────────────────────────────────────────

class TestBuildMp3Opts:
    def setup_method(self):
        self.orch = DownloadOrchestrator.__new__(DownloadOrchestrator)

    def test_format_is_bestaudio(self):
        opts = self.orch._build_mp3_opts("/tmp/stem")
        assert opts["format"] == "bestaudio/best"

    def test_postprocessor_is_mp3_320(self):
        opts = self.orch._build_mp3_opts("/tmp/stem")
        pp = opts["postprocessors"][0]
        assert pp["key"] == "FFmpegExtractAudio"
        assert pp["preferredcodec"] == "mp3"
        assert pp["preferredquality"] == "320"

    def test_noplaylist_is_true(self):
        opts = self.orch._build_mp3_opts("/tmp/stem")
        assert opts["noplaylist"] is True

    def test_output_template_uses_stem(self):
        opts = self.orch._build_mp3_opts("/tmp/mystem")
        assert "/tmp/mystem" in opts["outtmpl"]


# ── download_track() — P8: single failure never stops queue ──────────────────

class TestDownloadTrackIsolation:
    def test_all_tiers_fail_returns_false_not_raises(self, session):
        """P8: a single track failure must never raise an exception."""
        track = _make_track(session, "spotify:track:all_fail")
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._rate_limiter = MagicMock()
        orch._rate_limiter.is_healthy.return_value = True

        # Patch all tiers to raise exceptions
        with patch.object(orch, "_tier1_spotiflac", side_effect=Exception("tier1 boom")), \
             patch.object(orch, "_tier2_ytdlp_ytm", side_effect=Exception("tier2 boom")), \
             patch.object(orch, "_tier3_spotdl",    side_effect=Exception("tier3 boom")), \
             patch.object(orch, "_tier4_ytdlp_youtube", side_effect=Exception("tier4 boom")), \
             patch.object(orch, "_tier5_ytdlp_soundcloud", side_effect=Exception("tier5 boom")):
            result = orch.download_track(track, session)

        assert result is False  # must return False, not raise

    def test_successful_tier_returns_true_and_sets_downloaded(self, session):
        track = _make_track(session, "spotify:track:tier1_success")
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._rate_limiter = MagicMock()

        # The method label is derived from the filename: {uuid}_{service}.flac
        # split("_", 1) on "someuuid_qobuz" → ["someuuid", "qobuz"] → "spotiflac_qobuz"
        # Use a filename with exactly one underscore to get the right label
        fake_path = "/tmp/abc123_qobuz.flac"
        with patch.object(orch, "_tier1_spotiflac", return_value=fake_path), \
             patch.object(orch, "_record_attempt"):
            result = orch.download_track(track, session)

        assert result is True
        assert track.status == TrackStatus.DOWNLOADED.value
        assert track.download_method == "spotiflac_qobuz"

    def test_status_set_to_failed_after_give_up(self, session):
        track = _make_track(session, "spotify:track:give_up_test")
        # Pre-load 9 failed attempts
        _add_failed_attempts(session, track.id, 9)

        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._rate_limiter = MagicMock()

        with patch.object(orch, "_tier1_spotiflac", return_value=None), \
             patch.object(orch, "_tier2_ytdlp_ytm", return_value=None), \
             patch.object(orch, "_tier3_spotdl",    return_value=None), \
             patch.object(orch, "_tier4_ytdlp_youtube", return_value=None), \
             patch.object(orch, "_tier5_ytdlp_soundcloud", return_value=None):
            result = orch.download_track(track, session)

        assert result is False
        assert track.status == TrackStatus.FAILED.value
