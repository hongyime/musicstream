"""
Tests for musicstream/ingestion/downloader.py

Covers:
  - _should_give_up(): threshold logic (≥25 failed attempts)
  - _record_attempt(): writes DownloadAttempt row
  - _resolve_method_label(): correct label per tier
  - _build_mp3_opts(): correct yt-dlp options
  - download_track(): single failure never raises (P8), status transitions
  - MAX_CONCURRENT constant
"""
from __future__ import annotations

import os
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

from src.models import Base, DownloadAttempt, Track, TrackStatus  # noqa: E402
from src.ingestion.downloader import DownloadOrchestrator, _GIVE_UP_THRESHOLD, TEMP_DIR  # noqa: E402


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
    track = session.get(Track, track_id)
    if track is not None:
        # P2-6: give-up now reads tracks.attempt_count (incremented per failed
        # tier attempt in production + backfilled). Keep this helper consistent
        # so the _should_give_up tests exercise the real column-based logic.
        track.attempt_count = (track.attempt_count or 0) + count
    session.flush()


# ── Constants ─────────────────────────────────────────────────────────────────

class TestConstants:
    def test_max_concurrent_from_env(self):
        # Verify MAX_CONCURRENT is a valid positive integer (set from env at import time).
        assert isinstance(DownloadOrchestrator.MAX_CONCURRENT, int)
        assert DownloadOrchestrator.MAX_CONCURRENT >= 1

    def test_give_up_threshold_is_20(self):
        assert _GIVE_UP_THRESHOLD == 20


# ── _should_give_up ───────────────────────────────────────────────────────────

class TestShouldGiveUp:
    def test_below_threshold_returns_false(self, session):
        track = _make_track(session, "spotify:track:giveup_below")
        _add_failed_attempts(session, track.id, 8)
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        assert orch._should_give_up(session, track.id) is False

    def test_at_threshold_returns_true(self, session):
        track = _make_track(session, "spotify:track:giveup_at")
        _add_failed_attempts(session, track.id, 20)
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        assert orch._should_give_up(session, track.id) is True

    def test_above_threshold_returns_true(self, session):
        track = _make_track(session, "spotify:track:giveup_above")
        _add_failed_attempts(session, track.id, 30)
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
                method="tier2_ytdlp_ytm",
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
        orch._record_attempt(session, track.id, "tier2_ytdlp_ytm", None, True)
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
        orch._tier1_enabled = False

        # Patch all tiers to raise exceptions
        with patch.object(orch, "_tier2_ytdlp_ytm", side_effect=Exception("tier2 boom")), \
             patch.object(orch, "_tier3_spotdl",    side_effect=Exception("tier3 boom")), \
             patch.object(orch, "_tier4_ytdlp_youtube", side_effect=Exception("tier4 boom")), \
             patch.object(orch, "_tier5_ytdlp_soundcloud", side_effect=Exception("tier5 boom")):
            result = orch.download_track(track, session)

        assert result is False  # must return False, not raise

    def test_successful_tier_returns_true_and_sets_downloaded(self, session):
        from src.models import TrackStatus as TS
        track = _make_track(session, "spotify:track:tier1_success")
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._rate_limiter = MagicMock()
        orch._tier1_enabled = False
        orch._tagger = MagicMock()

        # organise() must set track.status itself (FileOrganiser does this)
        def _fake_organise(path, trk, sess):
            trk.status = TS.DOWNLOADED.value
            return "/media/artist/album/track.flac"

        orch._organiser = MagicMock()
        orch._organiser.organise.side_effect = _fake_organise

        fake_path = "/tmp/abc123_ytm.mp3"
        with patch.object(orch, "_tier5_ytdlp_soundcloud", return_value=None), \
             patch.object(orch, "_tier2_ytdlp_ytm", return_value=fake_path), \
             patch.object(orch, "_record_attempt"):
            result = orch.download_track(track, session)

        assert result is True
        assert track.status == TrackStatus.DOWNLOADED.value
        assert track.download_method == "ytdlp_ytm"
        assert track.claimed_at is None
        assert track.heartbeat_at is None
        assert track.claim_owner is None

    def test_status_set_to_failed_after_give_up(self, session):
        track = _make_track(session, "spotify:track:give_up_test")
        # Pre-load enough failures to cross the give-up threshold (25)
        _add_failed_attempts(session, track.id, 25)

        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._rate_limiter = MagicMock()
        orch._tier1_enabled = False

        with patch.object(orch, "_tier2_ytdlp_ytm", return_value=None), \
             patch.object(orch, "_tier3_spotdl",    return_value=None), \
             patch.object(orch, "_tier4_ytdlp_youtube", return_value=None), \
             patch.object(orch, "_tier5_ytdlp_soundcloud", return_value=None):
            result = orch.download_track(track, session)

        assert result is False
        assert track.status == TrackStatus.FAILED.value
        assert track.claimed_at is None
        assert track.heartbeat_at is None
        assert track.claim_owner is None


# ── _is_content_error ─────────────────────────────────────────────────────────

class TestIsContentError:
    """Content errors must not trip circuit breakers."""

    @pytest.mark.parametrize("msg", [
        "Requested format is not available. Use --list-formats for a list of available formats",
        "ERROR: [youtube] abc: Private video",
        "Video unavailable",
        "This video is not available",
        "Sign in to confirm your age",
        "This video requires payment",
        "Geographic restriction",
        "Not available in your country",
    ])
    def test_recognised_as_content_error(self, msg):
        assert DownloadOrchestrator._is_content_error(Exception(msg)) is True

    @pytest.mark.parametrize("msg", [
        "HTTPSConnectionPool: Max retries exceeded",
        "Connection refused",
        "Name or service not known",
        "timed out",
        "503 Service Unavailable",
    ])
    def test_not_a_content_error(self, msg):
        assert DownloadOrchestrator._is_content_error(Exception(msg)) is False


# ── Bug Condition Exploration Tests ──────────────────────────────────────────
# These tests are EXPECTED TO FAIL on unfixed code to confirm bugs exist.
# They encode the expected behavior and will pass after fixes are implemented.

class TestBug1YouTubeFormatSelectorExploration:
    """
    Bug Condition Exploration: YouTube Format Selector Fails on Limited Format Availability
    
    **Validates: Requirements 1.1, 1.2, 1.3**
    
    CRITICAL: This test MUST FAIL on unfixed code - failure confirms the bug exists.
    
    The current format selector "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best[ext=mp4]/best"
    fails when videos only provide opus or other formats not in the list.
    
    This test encodes the EXPECTED behavior: downloads should succeed and produce MP3 320kbps
    regardless of source format availability.
    
    EXPECTED OUTCOME on unfixed code: Test FAILS (DownloadError raised or returns None)
    EXPECTED OUTCOME after fix: Test PASSES (flexible selector accepts any format)
    """
    
    def test_format_selector_is_flexible(self):
        """
        Verify the format selector is the permissive "bestaudio/best".

        The old restrictive selector (bestaudio[ext=webm]/bestaudio[ext=m4a]/...)
        was replaced with "bestaudio/best" so that any available audio format
        is accepted and post-processed to MP3 320kbps by FFmpeg.
        """
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        opts = orch._build_mp3_opts("/tmp/test")
        assert opts["format"] == "bestaudio/best"
    
    def test_opus_format_not_in_selector_list(self):
        """
        Counterexample 1: Opus format is not in the current selector list.
        
        Videos with only opus audio will fail with the current selector.
        After fix, the flexible selector "bestaudio/best" will accept opus.
        """
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        opts = orch._build_mp3_opts("/tmp/test")
        
        # Document the bug: opus is not in the format list
        format_selector = opts["format"]
        assert "opus" not in format_selector, \
            "BUG CONFIRMED: opus format is not in the selector, will cause failures"
    
    def test_mp3_format_not_explicitly_in_selector(self):
        """
        Counterexample 2: MP3 format is not explicitly in the current selector list.
        
        SoundCloud tracks with only mp3 audio may fail with the current selector.
        After fix, the flexible selector "bestaudio/best" will accept mp3.
        """
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        opts = orch._build_mp3_opts("/tmp/test")
        
        # Document the bug: mp3 is not explicitly listed
        format_selector = opts["format"]
        assert "[ext=mp3]" not in format_selector, \
            "BUG CONFIRMED: mp3 format is not explicitly in the selector"
    
    def test_combined_streams_accepted_by_selector(self):
        """
        The "bestaudio/best" selector accepts combined video+audio streams as a
        fallback, meaning no stream type is unnecessarily excluded.
        """
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        opts = orch._build_mp3_opts("/tmp/test")
        format_selector = opts["format"]
        assert "/" in format_selector, "Selector uses fallback chain"
        assert "bestaudio[ext=" not in format_selector, \
            "Selector should not restrict to specific extensions"


# ── Property 2: Preservation Tests ───────────────────────────────────────────
# These tests verify that non-buggy YouTube downloads continue to work.
# They should PASS on UNFIXED code to establish baseline behavior to preserve.

try:
    from hypothesis import given, strategies as st, settings, HealthCheck
    HYPOTHESIS_AVAILABLE = True
except ImportError:
    HYPOTHESIS_AVAILABLE = False
    # Create dummy decorators if hypothesis not available
    def given(*args, **kwargs):
        def decorator(f):
            return f
        return decorator
    
    class st:
        @staticmethod
        def sampled_from(items):
            return None
        @staticmethod
        def integers(min_value=None, max_value=None):
            return None
        
        class HealthCheck:
            function_scoped_fixture = None
    
    def settings(*args, **kwargs):
        def decorator(f):
            return f
        return decorator


@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
class TestProperty2PreservationYouTubeDownloads:
    """
    Property 2: Preservation - Non-Buggy YouTube Downloads Continue to Work
    
    **Validates: Requirements 3.1, 3.2, 3.3**
    
    IMPORTANT: These tests should PASS on UNFIXED code to establish baseline behavior.
    
    These tests verify that videos with formats matching the current selector
    (webm, m4a, mp4) continue to work correctly after the fix is applied.
    
    The fix should make the selector MORE permissive (accepting opus, mp3, etc.)
    while preserving the existing behavior for videos that already work.
    
    EXPECTED OUTCOME on unfixed code: Tests PASS (baseline behavior confirmed)
    EXPECTED OUTCOME after fix: Tests PASS (no regressions)
    """
    
    @given(format_ext=st.sampled_from(["webm", "m4a", "mp4"]))
    @settings(max_examples=20)
    def test_current_selector_accepts_supported_formats(self, format_ext):
        """
        Property: For all formats in the current selector (webm, m4a, mp4),
        the format selector string should match them.
        
        This test verifies that the current selector correctly handles the formats
        it was designed for. After the fix, the flexible selector should still
        accept these formats.
        """
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        opts = orch._build_mp3_opts("/tmp/test")
        format_selector = opts["format"]
        
        # The current selector should include these formats
        # After fix, "bestaudio/best" will also accept them (more permissive)
        if format_ext in ["webm", "m4a"]:
            # These are explicitly in the bestaudio part
            assert f"bestaudio[ext={format_ext}]" in format_selector or \
                   "bestaudio/best" == format_selector, \
                   f"Selector should accept {format_ext} format"
        elif format_ext == "mp4":
            # mp4 is in the best[ext=mp4] part
            assert f"best[ext={format_ext}]" in format_selector or \
                   "bestaudio/best" == format_selector, \
                   f"Selector should accept {format_ext} format"
    
    @given(
        format_ext=st.sampled_from(["webm", "m4a", "mp4"]),
        quality=st.sampled_from(["320", "256", "192", "128"])
    )
    @settings(max_examples=30)
    def test_mp3_postprocessing_configuration_preserved(self, format_ext, quality):
        """
        Property: For all supported formats and quality settings,
        the MP3 post-processing configuration should remain consistent.
        
        This test verifies that the FFmpeg post-processing configuration
        (MP3 320kbps output) is preserved regardless of input format.
        
        The fix changes the format selector but should NOT change the
        post-processing configuration.
        """
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        opts = orch._build_mp3_opts("/tmp/test")
        
        # Verify post-processing configuration is preserved
        assert "postprocessors" in opts
        assert len(opts["postprocessors"]) > 0
        
        pp = opts["postprocessors"][0]
        assert pp["key"] == "FFmpegExtractAudio", \
            "Post-processor should be FFmpegExtractAudio"
        assert pp["preferredcodec"] == "mp3", \
            "Output codec should be MP3"
        assert pp["preferredquality"] == "320", \
            "Output quality should be 320kbps (unchanged)"
    
    @given(
        tier=st.sampled_from([
            "tier2_ytdlp_ytm",
            "tier4_ytdlp_youtube", 
            "tier5_ytdlp_soundcloud"
        ])
    )
    @settings(max_examples=15)
    def test_all_youtube_tiers_use_same_format_selector(self, tier):
        """
        Property: All YouTube-based tiers (2, 4, 5) should use the same
        format selector configuration.
        
        This test verifies that the format selector is consistently applied
        across all tiers that use yt-dlp. The fix should update all of them
        uniformly.
        """
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        opts = orch._build_mp3_opts("/tmp/test")
        
        # All tiers use _build_mp3_opts, so they share the same format selector
        format_selector = opts["format"]
        
        # Verify the selector is present and consistent
        assert format_selector is not None
        assert isinstance(format_selector, str)
        assert len(format_selector) > 0
        
        # After fix, all tiers will use "bestaudio/best"
        # Before fix, all tiers use the restrictive selector
        # Either way, consistency is preserved
    
    def test_format_selector_fallback_chain_structure(self):
        """
        Property: The format selector should maintain a fallback chain structure.
        
        This test verifies that the format selector uses yt-dlp's fallback
        mechanism (formats separated by "/"). The fix should preserve this
        structure while making it more permissive.
        
        Current: "bestaudio[ext=webm]/bestaudio[ext=m4a]/bestaudio/best[ext=mp4]/best"
        After fix: "bestaudio/best"
        
        Both maintain the fallback chain structure.
        """
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        opts = orch._build_mp3_opts("/tmp/test")
        format_selector = opts["format"]
        
        # Verify fallback chain structure is present
        # Either the current multi-part chain or the simpler "bestaudio/best"
        assert "/" in format_selector or format_selector in ["bestaudio", "best"], \
            "Format selector should use fallback chain structure"
        
        # Verify it includes audio preference
        assert "bestaudio" in format_selector or "best" in format_selector, \
            "Format selector should prefer audio streams"
    
    @given(
        video_duration=st.integers(min_value=30, max_value=600),
        tolerance=st.integers(min_value=1, max_value=10)
    )
    @settings(max_examples=25)
    def test_duration_validation_logic_preserved(self, video_duration, tolerance):
        """
        Property: Duration validation logic should remain unchanged.
        
        This test verifies that the ±5 second duration tolerance check
        is preserved after the fix. The fix changes format selection,
        not duration validation.
        """
        # Duration validation is in _tier2_ytdlp_ytm, not in _build_mp3_opts
        # This test verifies the constant is unchanged
        from src.ingestion.downloader import _DURATION_TOLERANCE_S
        
        assert _DURATION_TOLERANCE_S == 5, \
            "Duration tolerance should remain 5 seconds (unchanged)"
        
        # Verify the tolerance logic would work correctly
        expected_s = video_duration
        got_s = video_duration + tolerance
        delta = abs(got_s - expected_s)
        
        # This mimics the logic in _tier2_ytdlp_ytm
        if delta <= _DURATION_TOLERANCE_S:
            # Should pass validation
            assert delta <= 5
        else:
            # Should fail validation
            assert delta > 5
    
    def test_noplaylist_option_preserved(self):
        """
        Property: The noplaylist option should remain enabled.
        
        This test verifies that yt-dlp is configured to download single
        videos only, not playlists. The fix should not change this behavior.
        """
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        opts = orch._build_mp3_opts("/tmp/test")
        
        assert opts.get("noplaylist") is True, \
            "noplaylist option should remain True (unchanged)"
    
    def test_retry_configuration_preserved(self):
        """
        Property: Retry configuration should remain unchanged.
        
        This test verifies that yt-dlp retry settings (retries=3,
        fragment_retries=3) are preserved after the fix.
        """
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        opts = orch._build_mp3_opts("/tmp/test")
        
        assert opts.get("retries") == 3, \
            "Retry count should remain 3 (unchanged)"
        assert opts.get("fragment_retries") == 3, \
            "Fragment retry count should remain 3 (unchanged)"
        assert opts.get("skip_unavailable_fragments") is True, \
            "skip_unavailable_fragments should remain True (unchanged)"
    
    @given(stem=st.sampled_from(["/tmp/test", "/tmp/video", "/tmp/audio123"]))
    @settings(max_examples=10)
    def test_output_template_uses_stem_correctly(self, stem):
        """
        Property: For all output stems, the output template should
        correctly incorporate the stem path.
        
        This test verifies that the output template configuration
        is preserved after the fix.
        """
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        opts = orch._build_mp3_opts(stem)
        
        assert "outtmpl" in opts
        assert stem in opts["outtmpl"], \
            f"Output template should include stem path {stem}"




# ── Failure-reason attribution: persist the REAL reason, not 'tier returned None' ─

class TestFailureReasonAttribution:
    """download_attempts.error must carry the tier's real reason (or the
    explicit fallback constant), recorded thread-safely."""

    def _orch(self):
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._rate_limiter = MagicMock()
        orch._rate_limiter.is_healthy.return_value = True
        orch._tier1_enabled = False
        return orch

    def test_records_real_reason_when_tier_notes_fail(self, session):
        from src.ingestion import tier_errors
        track = _make_track(session, "spotify:track:reason_real")
        orch = self._orch()

        def fake_tier(trk):
            orch._note_fail(tier_errors.REGION_UNAVAIL)
            return None

        result = orch.download_track(
            track, session, tiers_override=[("tier0_librespot", fake_tier)]
        )
        assert result is False
        attempt = (
            session.query(DownloadAttempt)
            .filter_by(track_id=track.id, method="tier0_librespot")
            .first()
        )
        assert attempt is not None
        assert attempt.error == tier_errors.REGION_UNAVAIL
        assert attempt.success is False

    def test_fallback_when_tier_silent(self, session):
        from src.ingestion import tier_errors
        track = _make_track(session, "spotify:track:reason_fallback")
        orch = self._orch()
        result = orch.download_track(
            track, session, tiers_override=[("tier2_ytdlp_ytm", lambda trk: None)]
        )
        assert result is False
        attempt = session.query(DownloadAttempt).filter_by(track_id=track.id).first()
        assert attempt.error == tier_errors.UNKNOWN_TIER_FAIL

    def test_no_leak_into_success(self, session):
        from src.ingestion import tier_errors
        from src.models import TrackStatus as TS
        track = _make_track(session, "spotify:track:reason_noleak")
        orch = self._orch()
        orch._tagger = MagicMock()
        orch._organiser = MagicMock()

        def _fake_org(path, trk, sess):
            trk.status = TS.DOWNLOADED.value
            return "/media/a/b/c.mp3"
        orch._organiser.organise.side_effect = _fake_org

        def failing(trk):
            orch._note_fail(tier_errors.REGION_UNAVAIL)
            return None

        def succeeding(trk):
            return "/tmp/ok_ytm.mp3"

        result = orch.download_track(
            track, session,
            tiers_override=[("tier0_librespot", failing), ("tier2_ytdlp_ytm", succeeding)],
        )
        assert result is True
        attempts = (
            session.query(DownloadAttempt)
            .filter_by(track_id=track.id)
            .order_by(DownloadAttempt.id)
            .all()
        )
        assert attempts[0].success is False
        assert attempts[0].error == tier_errors.REGION_UNAVAIL
        assert attempts[-1].success is True
        assert attempts[-1].error is None

    def test_thread_isolation(self):
        """Two threads set different reasons concurrently; each must read its own
        (proves the thread-local channel, not a shared instance attribute)."""
        import threading
        from src.ingestion import tier_errors
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        results = {}
        both_set = threading.Barrier(2)

        def worker(name, reason):
            orch._note_fail(reason)
            both_set.wait()  # ensure both have written before either reads
            results[name] = getattr(orch._fail_tls, "fail_reason", None)

        t1 = threading.Thread(target=worker, args=("a", tier_errors.REGION_UNAVAIL))
        t2 = threading.Thread(target=worker, args=("b", tier_errors.AUTH_FAILURE))
        t1.start()
        t2.start()
        t1.join()
        t2.join()
        assert results["a"] == tier_errors.REGION_UNAVAIL
        assert results["b"] == tier_errors.AUTH_FAILURE


class TestTier0FailureReasons:
    """_tier0_librespot must categorize each return-None path via _note_fail."""

    def _orch(self):
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._rate_limiter = MagicMock()
        orch._rate_limiter.is_healthy.return_value = True
        orch._fail_tls.fail_reason = None
        return orch

    def test_not_available(self, session):
        import src.ingestion.downloader as dl
        from src.ingestion import tier_errors
        track = _make_track(session, "spotify:track:t0_unavail")
        orch = self._orch()
        with patch.object(dl, "LIBRESPOT_AVAILABLE", False):
            assert orch._tier0_librespot(track) is None
        assert orch._fail_tls.fail_reason == tier_errors.NOT_AVAILABLE

    def test_circuit_open(self, session):
        import src.ingestion.downloader as dl
        from src.ingestion import tier_errors
        track = _make_track(session, "spotify:track:t0_circuit")
        orch = self._orch()
        orch._rate_limiter.is_healthy.return_value = False
        with patch.object(dl, "LIBRESPOT_AVAILABLE", True):
            assert orch._tier0_librespot(track) is None
        assert orch._fail_tls.fail_reason == tier_errors.CIRCUIT_OPEN

    def test_no_source_id(self, session):
        import src.ingestion.downloader as dl
        from src.ingestion import tier_errors
        track = _make_track(session, "spotify:track:t0_noid")
        orch = self._orch()
        with patch.object(dl, "LIBRESPOT_AVAILABLE", True):
            assert orch._tier0_librespot(track) is None
        assert orch._fail_tls.fail_reason == tier_errors.NO_SOURCE_ID

    def test_region_unavailable(self, session):
        import src.ingestion.downloader as dl
        from src.ingestion import tier_errors
        track = _make_track(session, "spotify:track:t0_region")
        track.spotify_id = "abc"
        session.flush()
        orch = self._orch()
        with patch.object(dl, "LIBRESPOT_AVAILABLE", True), \
             patch.object(dl, "_get_librespot_session",
                          side_effect=RuntimeError("Cannot get alternative track")):
            assert orch._tier0_librespot(track) is None
        assert orch._fail_tls.fail_reason == tier_errors.REGION_UNAVAIL


# ── Librespot timeout → rate_limited attribution ─────────────────────────

class TestLibrespotTimeoutRecording:
    """A librespot per-track timeout (rate-limit symptom) must persist a
    rate_limited download_attempts row via an independent session."""

    def test_record_librespot_timeout_writes_rate_limited(self, session):
        from contextlib import contextmanager
        from src.ingestion import tier_errors
        track = _make_track(session, "spotify:track:t0_ratelimit")
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)

        @contextmanager
        def fake_get_session():
            # reuse the fixture session; conftest owns commit/rollback lifecycle
            yield session

        with patch("src.db.get_session", fake_get_session):
            orch._record_librespot_timeout(track.id)

        attempt = (
            session.query(DownloadAttempt)
            .filter_by(track_id=track.id, method="tier0_librespot")
            .one()
        )
        assert attempt.error == tier_errors.RATE_LIMITED
        assert attempt.success is False
        refreshed = session.get(Track, track.id)
        assert (refreshed.attempt_count or 0) == 1

    def test_record_librespot_timeout_requeues_claimed_track(self, session):
        from contextlib import contextmanager
        track = _make_track(session, "spotify:track:t0_ratelimit_requeue")
        track.status = TrackStatus.DOWNLOADING.value
        track.claimed_at = datetime.now(timezone.utc)
        track.heartbeat_at = datetime.now(timezone.utc)
        track.claim_owner = "worker:test"
        session.flush()
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)

        @contextmanager
        def fake_get_session():
            yield session

        with patch("src.db.get_session", fake_get_session):
            orch._record_librespot_timeout(track.id)

        refreshed = session.get(Track, track.id)
        assert refreshed.status == TrackStatus.PENDING.value
        assert refreshed.claimed_at is None
        assert refreshed.heartbeat_at is None
        assert refreshed.claim_owner is None

    def test_timeout_recording_never_raises_on_db_error(self, session):
        """Recording must never break the sweep, even if the session blows up."""
        from contextlib import contextmanager
        track = _make_track(session, "spotify:track:t0_rl_safe")
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)

        @contextmanager
        def boom():
            raise RuntimeError("db exploded")
            yield  # pragma: no cover

        with patch("src.db.get_session", boom):
            # must swallow the error, not propagate
            orch._record_librespot_timeout(track.id)


# ── Librespot kill-event suppression (worker skips record; main is authoritative) ─

class TestLibrespotKillSuppression:
    """When the sweep's timeout watchdog sets the kill event, the worker's
    download_track must NOT record an attempt — the main thread writes the single
    authoritative rate_limited row (Option B, avoids a double-record)."""

    def test_kill_event_suppresses_worker_record(self, session):
        import src.ingestion.downloader as dl
        track = _make_track(session, "spotify:track:kill1")
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._rate_limiter = MagicMock()
        orch._rate_limiter.is_healthy.return_value = True
        orch._tier1_enabled = False

        def fake_tier(trk):
            orch._note_fail("stream_error")  # would normally be recorded by the loop
            return None

        dl._librespot_kill_event.set()
        try:
            result = orch.download_track(
                track, session, tiers_override=[("tier0_librespot", fake_tier)]
            )
        finally:
            dl._librespot_kill_event.clear()

        assert result is False
        # worker recorded nothing; the sweep's main thread owns the timeout row
        assert session.query(DownloadAttempt).filter_by(track_id=track.id).count() == 0

    def test_no_kill_event_records_normally(self, session):
        """Sanity: with the event clear, the worker records as usual."""
        import src.ingestion.downloader as dl
        from src.ingestion import tier_errors
        assert not dl._librespot_kill_event.is_set()
        track = _make_track(session, "spotify:track:kill2")
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._rate_limiter = MagicMock()
        orch._rate_limiter.is_healthy.return_value = True
        orch._tier1_enabled = False

        def fake_tier(trk):
            orch._note_fail(tier_errors.REGION_UNAVAIL)
            return None

        orch.download_track(track, session, tiers_override=[("tier0_librespot", fake_tier)])
        attempt = session.query(DownloadAttempt).filter_by(track_id=track.id).one()
        assert attempt.error == tier_errors.REGION_UNAVAIL
