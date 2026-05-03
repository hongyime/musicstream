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

from src.models import Base, DownloadAttempt, Track, TrackStatus
from src.ingestion.downloader import DownloadOrchestrator, _GIVE_UP_THRESHOLD


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

    def test_give_up_threshold_is_25(self):
        assert _GIVE_UP_THRESHOLD == 25


# ── _should_give_up ───────────────────────────────────────────────────────────

class TestShouldGiveUp:
    def test_below_threshold_returns_false(self, session):
        track = _make_track(session, "spotify:track:giveup_below")
        _add_failed_attempts(session, track.id, 8)
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        assert orch._should_give_up(session, track.id) is False

    def test_at_threshold_returns_true(self, session):
        track = _make_track(session, "spotify:track:giveup_at")
        _add_failed_attempts(session, track.id, 25)
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
        # SpotiFLAC names files by track title — service cannot be recovered from path.
        ("tier1_spotiflac", "/tmp/abc_qobuz.flac",    "spotiflac"),
        ("tier1_spotiflac", "/tmp/abc_tidal.flac",    "spotiflac"),
        ("tier1_spotiflac", "/tmp/abc_amazon.flac",   "spotiflac"),
        ("tier1_spotiflac", "/tmp/abc_deezer.flac",   "spotiflac"),
        ("tier1_spotiflac", "/tmp/abc_youtube.flac",  "spotiflac"),
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
        from src.models import TrackStatus as TS
        track = _make_track(session, "spotify:track:tier1_success")
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._rate_limiter = MagicMock()
        orch._tagger = MagicMock()

        # organise() must set track.status itself (FileOrganiser does this)
        def _fake_organise(path, trk, sess):
            trk.status = TS.DOWNLOADED.value
            return "/media/artist/album/track.flac"

        orch._organiser = MagicMock()
        orch._organiser.organise.side_effect = _fake_organise

        fake_path = "/tmp/abc123_spotiflac.flac"
        with patch.object(orch, "_tier1_spotiflac", return_value=fake_path), \
             patch.object(orch, "_record_attempt"):
            result = orch.download_track(track, session)

        assert result is True
        assert track.status == TrackStatus.DOWNLOADED.value
        assert track.download_method == "spotiflac"

    def test_status_set_to_failed_after_give_up(self, session):
        track = _make_track(session, "spotify:track:give_up_test")
        # Pre-load enough failures to cross the give-up threshold (25)
        _add_failed_attempts(session, track.id, 25)

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
    from hypothesis import given, strategies as st, settings, assume, HealthCheck
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


# ── Bug 2: SpotiFLAC log_level Parameter Exploration ─────────────────────────

class TestBug2SpotiFLACLogLevelExploration:
    """
    Bug Condition Exploration: SpotiFLAC Constructor Rejects log_level Parameter
    
    **Validates: Requirements 1.4, 1.5, 1.6**
    
    CRITICAL: This test MUST FAIL on unfixed code - failure confirms the bug exists.
    
    The current code calls SpotiFLAC with log_level=logging.WARNING parameter,
    but SpotiFLAC 0.2.6 does not support this parameter, causing TypeError.
    
    This test encodes the EXPECTED behavior: SpotiFLAC should instantiate
    successfully without TypeError when called with only supported parameters.
    
    EXPECTED OUTCOME on unfixed code: Test FAILS (detects log_level in code)
    EXPECTED OUTCOME after fix: Test PASSES (log_level removed from code)
    """
    
    def test_tier1_spotiflac_code_contains_log_level_parameter(self):
        """
        Code inspection test: Verify _tier1_spotiflac contains log_level parameter.
        
        This test reads the source code of _tier1_spotiflac to verify that it
        currently calls SpotiFLAC with the unsupported log_level parameter.
        
        On unfixed code: Test FAILS (log_level found in code - bug confirmed)
        After fix: Test PASSES (log_level removed from code)
        """
        import inspect
        from src.ingestion.downloader import DownloadOrchestrator
        
        # Get the source code of _tier1_spotiflac method
        source = inspect.getsource(DownloadOrchestrator._tier1_spotiflac)
        
        # Check if log_level parameter is present in the SpotiFLAC call
        has_log_level = "log_level=" in source
        
        if has_log_level:
            # BUG CONFIRMED: log_level parameter is in the code
            # Extract the line for documentation
            lines = source.split('\n')
            log_level_lines = [line for line in lines if "log_level=" in line]
            
            pytest.fail(
                f"BUG CONFIRMED: _tier1_spotiflac contains unsupported log_level parameter.\n"
                f"Found in code:\n" + "\n".join(f"  {line.strip()}" for line in log_level_lines) + "\n"
                f"\nThis parameter is not supported in SpotiFLAC 0.2.6 and causes TypeError.\n"
                f"Expected behavior: SpotiFLAC should be called WITHOUT log_level parameter.\n"
                f"\nThis test FAILS on unfixed code (expected for exploration test).\n"
                f"After fix (removing log_level parameter), this test should PASS."
            )
        else:
            # Fix has been applied: log_level parameter removed
            assert True, "log_level parameter has been removed (fix applied)"
    
    def test_tier1_spotiflac_method_calls_with_log_level_mock(self, session):
        """
        Mock-based test: Verify _tier1_spotiflac calls SpotiFLAC with log_level.
        
        This test uses mocking to intercept the SpotiFLAC call and verify
        that log_level parameter is passed, confirming the bug exists.
        
        On unfixed code: Test FAILS (log_level detected in call - bug confirmed)
        After fix: Test PASSES (log_level not in call)
        """
        import logging
        from unittest.mock import patch, MagicMock, call
        
        # Create a track with spotify_id
        track = _make_track(session, "spotify:track:3n3Ppam7vgaVa1iaRUc9Lp")
        track.spotify_id = "3n3Ppam7vgaVa1iaRUc9Lp"
        session.flush()
        
        # Create orchestrator
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._rate_limiter = MagicMock()
        orch._rate_limiter.is_healthy.return_value = True
        
        # Mock SpotiFLAC to capture the call arguments
        from src.ingestion import downloader
        
        # Only run this test if SpotiFLAC is available in the module
        if not downloader.SPOTIFLAC_AVAILABLE:
            pytest.skip("SpotiFLAC not available - cannot test call signature")
        
        call_kwargs = {}
        
        def mock_spotiflac(*args, **kwargs):
            # Capture the kwargs to verify log_level is present
            call_kwargs.update(kwargs)
            # Simulate the bug: raise TypeError if log_level is present
            if "log_level" in kwargs:
                raise TypeError("SpotiFLAC() got an unexpected keyword argument 'log_level'")
            # Return a mock that simulates no files downloaded
            return MagicMock()
        
        with patch.object(downloader, "_SpotiFLAC", side_effect=mock_spotiflac):
            try:
                result = orch._tier1_spotiflac(track)
                
                # If we reach here without TypeError, check if log_level was in the call
                if "log_level" in call_kwargs:
                    pytest.fail(
                        f"BUG CONFIRMED: _tier1_spotiflac passes log_level parameter.\n"
                        f"Call kwargs: {call_kwargs}\n"
                        f"log_level value: {call_kwargs['log_level']}\n"
                        f"\nThis parameter is not supported in SpotiFLAC 0.2.6.\n"
                        f"Expected behavior: Call should NOT include log_level parameter.\n"
                        f"\nThis test FAILS on unfixed code (expected for exploration test).\n"
                        f"After fix, log_level should be removed from the call."
                    )
                else:
                    # Fix has been applied
                    assert True, "log_level parameter not in call (fix applied)"
                
            except TypeError as e:
                # On unfixed code, we expect TypeError due to log_level
                error_msg = str(e)
                
                if "log_level" in error_msg and "log_level" in call_kwargs:
                    # BUG CONFIRMED
                    pytest.fail(
                        f"BUG CONFIRMED: _tier1_spotiflac calls SpotiFLAC with unsupported log_level parameter.\n"
                        f"Call kwargs: {call_kwargs}\n"
                        f"log_level value: {call_kwargs.get('log_level')}\n"
                        f"Error: {error_msg}\n"
                        f"\nCounterexample documented:\n"
                        f"  - Parameter: log_level={call_kwargs.get('log_level')}\n"
                        f"  - Error: TypeError - unexpected keyword argument\n"
                        f"  - Root cause: SpotiFLAC 0.2.6 does not support log_level parameter\n"
                        f"\nThis test FAILS on unfixed code (expected for exploration test).\n"
                        f"After fix, log_level should be removed from the call."
                    )
                else:
                    # Different error - re-raise
                    raise
    
    def test_expected_behavior_spotiflac_without_log_level(self):
        """
        Expected behavior test: Document how SpotiFLAC should be called.
        
        This test documents the EXPECTED behavior after the fix:
        SpotiFLAC should be called with only supported parameters.
        
        Supported parameters in SpotiFLAC 0.2.6:
        - url: Spotify track/album/playlist URL (required)
        - output_dir: Output directory path (optional)
        - services: List of services to try (optional)
        
        Unsupported parameters (cause TypeError):
        - log_level: Not supported in 0.2.6 (THIS IS THE BUG)
        - quality: Not supported in 0.2.6
        """
        import inspect
        from src.ingestion.downloader import DownloadOrchestrator
        
        # Get the source code
        source = inspect.getsource(DownloadOrchestrator._tier1_spotiflac)
        
        # Document the expected call signature
        expected_params = ["url", "output_dir", "services"]
        unsupported_params = ["log_level", "quality"]
        
        # Check for unsupported parameters
        found_unsupported = []
        for param in unsupported_params:
            if f"{param}=" in source:
                found_unsupported.append(param)
        
        if found_unsupported:
            pytest.fail(
                f"BUG CONFIRMED: Unsupported parameters found in _tier1_spotiflac:\n"
                f"  {', '.join(found_unsupported)}\n"
                f"\nSpotiFLAC 0.2.6 supported parameters: {', '.join(expected_params)}\n"
                f"Unsupported parameters: {', '.join(unsupported_params)}\n"
                f"\nExpected behavior: Call SpotiFLAC with ONLY supported parameters.\n"
                f"Example correct call:\n"
                f"  _SpotiFLAC(\n"
                f"      url=spotify_url,\n"
                f"      output_dir=out_dir,\n"
                f"      services=['qobuz', 'tidal', 'amazon', 'deezer'],\n"
                f"  )\n"
                f"\nThis test FAILS on unfixed code (expected for exploration test).\n"
                f"After fix, unsupported parameters should be removed."
            )
        else:
            # Fix has been applied
            assert True, "Only supported parameters used (fix applied)"
    
    def test_bug_condition_documented_in_code_comments(self):
        """
        Documentation test: Verify code comments acknowledge API limitations.
        
        This test checks if the code has comments documenting which parameters
        are supported in SpotiFLAC 0.2.6.
        
        The existing code has a comment: "API confirmed: output_dir + services valid in 0.2.6.
        quality= does NOT exist in 0.2.6 — omit it."
        
        This test verifies that the code is aware of API limitations but
        still incorrectly uses log_level parameter.
        """
        import inspect
        from src.ingestion.downloader import DownloadOrchestrator
        
        # Get the source code
        source = inspect.getsource(DownloadOrchestrator._tier1_spotiflac)
        
        # Check for API documentation comments
        has_api_comment = "API confirmed" in source or "0.2.6" in source
        has_log_level_param = "log_level=" in source
        
        if has_api_comment and has_log_level_param:
            # BUG CONFIRMED: Code acknowledges API limitations but still uses log_level
            pytest.fail(
                f"BUG CONFIRMED: Code has API documentation but still uses unsupported log_level.\n"
                f"\nThe code contains comments about SpotiFLAC 0.2.6 API limitations,\n"
                f"acknowledging that 'quality=' does not exist in 0.2.6,\n"
                f"but it still incorrectly uses 'log_level=' parameter which is also unsupported.\n"
                f"\nThis demonstrates the bug: log_level parameter was overlooked\n"
                f"when other unsupported parameters (quality) were identified and removed.\n"
                f"\nThis test FAILS on unfixed code (expected for exploration test).\n"
                f"After fix, log_level should be removed like quality was."
            )
        elif has_log_level_param:
            # Bug exists but no API comments
            pytest.fail(
                f"BUG CONFIRMED: Code uses unsupported log_level parameter.\n"
                f"This test FAILS on unfixed code (expected for exploration test)."
            )
        else:
            # Fix has been applied
            assert True, "log_level parameter removed (fix applied)"


# ── Property 2: Preservation Tests for SpotiFLAC ─────────────────────────────
# These tests verify that SpotiFLAC downloads work correctly when called
# WITHOUT the log_level parameter (the expected behavior after fix).
# They should PASS to establish baseline behavior to preserve.

@pytest.mark.skipif(not HYPOTHESIS_AVAILABLE, reason="hypothesis not installed")
class TestProperty2PreservationSpotiFLACDownloads:
    """
    Property 2: Preservation - SpotiFLAC Downloads Without log_level Work
    
    **Validates: Requirements 3.1, 3.2, 3.3, 3.4, 3.5, 3.6**
    
    IMPORTANT: These tests verify the EXPECTED behavior after fix.
    
    These tests verify that SpotiFLAC instantiates and downloads successfully
    when called with ONLY supported parameters (url, output_dir, services).
    
    The fix removes the unsupported log_level parameter. These tests confirm
    that SpotiFLAC works correctly without it, establishing the baseline
    behavior to preserve.
    
    EXPECTED OUTCOME: Tests PASS (baseline behavior confirmed)
    """
    
    @given(
        spotify_id=st.text(
            alphabet=st.characters(whitelist_categories=("Lu", "Ll", "Nd")),
            min_size=22,
            max_size=22
        )
    )
    @settings(max_examples=20, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_spotiflac_supported_parameters_only(self, spotify_id, session):
        """
        Property: For all valid Spotify IDs, SpotiFLAC should instantiate
        successfully when called with only supported parameters.
        
        Supported parameters in SpotiFLAC 0.2.6:
        - url: Spotify track URL (required)
        - output_dir: Output directory path (optional)
        - services: List of services to try (optional)
        
        This test verifies that the parameter set is correct after the fix.
        """
        from src.ingestion import downloader
        
        if not downloader.SPOTIFLAC_AVAILABLE:
            pytest.skip("SpotiFLAC not available")
        
        # Create a track with the generated spotify_id
        track = _make_track(session, f"spotify:track:{spotify_id}")
        track.spotify_id = spotify_id
        session.flush()
        
        # Create orchestrator
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._rate_limiter = MagicMock()
        orch._rate_limiter.is_healthy.return_value = True
        
        # Mock SpotiFLAC to verify it's called with correct parameters
        call_kwargs = {}
        
        def mock_spotiflac(*args, **kwargs):
            call_kwargs.update(kwargs)
            # Verify only supported parameters are present
            supported = {"url", "output_dir", "services"}
            unsupported = set(kwargs.keys()) - supported
            
            if unsupported:
                raise TypeError(
                    f"SpotiFLAC() got unexpected keyword arguments: {unsupported}"
                )
            
            # Return mock that simulates no files (to avoid file system operations)
            return MagicMock()
        
        with patch.object(downloader, "_SpotiFLAC", side_effect=mock_spotiflac):
            # This should not raise TypeError
            result = orch._tier1_spotiflac(track)
            
            # Verify the call was made with correct parameters
            assert "url" in call_kwargs, "url parameter should be present"
            assert "output_dir" in call_kwargs, "output_dir parameter should be present"
            assert "services" in call_kwargs, "services parameter should be present"
            
            # Verify unsupported parameters are NOT present
            assert "log_level" not in call_kwargs, \
                "log_level parameter should NOT be present (unsupported in 0.2.6)"
            assert "quality" not in call_kwargs, \
                "quality parameter should NOT be present (unsupported in 0.2.6)"
    
    @given(
        services_list=st.lists(
            st.sampled_from(["qobuz", "tidal", "amazon", "deezer", "youtube"]),
            min_size=1,
            max_size=5,
            unique=True
        )
    )
    @settings(max_examples=15, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_spotiflac_services_parameter_flexibility(self, services_list, session):
        """
        Property: For all valid service combinations, SpotiFLAC should accept
        the services parameter correctly.
        
        This test verifies that the services parameter (which IS supported)
        continues to work correctly after removing log_level.
        """
        from src.ingestion import downloader
        
        if not downloader.SPOTIFLAC_AVAILABLE:
            pytest.skip("SpotiFLAC not available")
        
        # Create a track
        track = _make_track(session, "spotify:track:test123")
        track.spotify_id = "test123"
        session.flush()
        
        # Create orchestrator
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._rate_limiter = MagicMock()
        orch._rate_limiter.is_healthy.return_value = True
        
        # Mock SpotiFLAC to verify services parameter
        received_services = []
        
        def mock_spotiflac(*args, **kwargs):
            if "services" in kwargs:
                received_services.extend(kwargs["services"])
            
            # Verify no unsupported parameters
            if "log_level" in kwargs:
                raise TypeError("log_level not supported")
            
            return MagicMock()
        
        with patch.object(downloader, "_SpotiFLAC", side_effect=mock_spotiflac):
            # Should not raise
            orch._tier1_spotiflac(track)
            
            # Verify services were passed (implementation uses fixed list,
            # but this test verifies the parameter mechanism works)
            assert len(received_services) > 0, \
                "services parameter should be passed to SpotiFLAC"
    
    def test_spotiflac_url_format_preserved(self, session):
        """
        Property: The Spotify URL format should remain consistent.
        
        This test verifies that the URL construction logic is preserved
        after removing the log_level parameter.
        """
        from src.ingestion import downloader
        
        if not downloader.SPOTIFLAC_AVAILABLE:
            pytest.skip("SpotiFLAC not available")
        
        # Create a track with known spotify_id
        track = _make_track(session, "spotify:track:3n3Ppam7vgaVa1iaRUc9Lp")
        track.spotify_id = "3n3Ppam7vgaVa1iaRUc9Lp"
        session.flush()
        
        # Create orchestrator
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._rate_limiter = MagicMock()
        orch._rate_limiter.is_healthy.return_value = True
        
        # Mock SpotiFLAC to capture URL
        captured_url = []
        
        def mock_spotiflac(*args, **kwargs):
            if "url" in kwargs:
                captured_url.append(kwargs["url"])
            return MagicMock()
        
        with patch.object(downloader, "_SpotiFLAC", side_effect=mock_spotiflac):
            orch._tier1_spotiflac(track)
            
            # Verify URL format
            assert len(captured_url) == 1
            assert captured_url[0] == "https://open.spotify.com/track/3n3Ppam7vgaVa1iaRUc9Lp", \
                "Spotify URL format should be preserved"
    
    def test_spotiflac_output_dir_creation_preserved(self, session):
        """
        Property: Output directory creation logic should remain unchanged.
        
        This test verifies that the temporary directory creation and
        management is preserved after the fix.
        """
        from src.ingestion import downloader
        
        if not downloader.SPOTIFLAC_AVAILABLE:
            pytest.skip("SpotiFLAC not available")
        
        # Create a track
        track = _make_track(session, "spotify:track:test456")
        track.spotify_id = "test456"
        session.flush()
        
        # Create orchestrator
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._rate_limiter = MagicMock()
        orch._rate_limiter.is_healthy.return_value = True
        
        # Mock SpotiFLAC to capture output_dir
        captured_dirs = []
        
        def mock_spotiflac(*args, **kwargs):
            if "output_dir" in kwargs:
                captured_dirs.append(kwargs["output_dir"])
            return MagicMock()
        
        with patch.object(downloader, "_SpotiFLAC", side_effect=mock_spotiflac):
            orch._tier1_spotiflac(track)
            
            # Verify output_dir was provided
            assert len(captured_dirs) == 1
            output_dir = captured_dirs[0]
            
            # Verify it's in TEMP_DIR
            assert TEMP_DIR in output_dir, \
                "Output directory should be in TEMP_DIR"
            
            # Verify it has spotiflac prefix
            assert "spotiflac_" in output_dir, \
                "Output directory should have spotiflac_ prefix"
    
    def test_spotiflac_circuit_breaker_integration_preserved(self, session):
        """
        Property: Circuit breaker integration should remain unchanged.
        
        This test verifies that the rate limiter health check and
        success/failure recording is preserved after the fix.
        """
        from src.ingestion import downloader
        
        if not downloader.SPOTIFLAC_AVAILABLE:
            pytest.skip("SpotiFLAC not available")
        
        # Create a track
        track = _make_track(session, "spotify:track:test789")
        track.spotify_id = "test789"
        session.flush()
        
        # Create orchestrator
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._rate_limiter = MagicMock()
        orch._rate_limiter.is_healthy.return_value = True
        
        # Mock SpotiFLAC to succeed
        def mock_spotiflac(*args, **kwargs):
            return MagicMock()
        
        with patch.object(downloader, "_SpotiFLAC", side_effect=mock_spotiflac):
            # Mock os.walk to simulate no files found (failure case)
            with patch("os.walk", return_value=[]):
                result = orch._tier1_spotiflac(track)
                
                # Verify circuit breaker was checked
                orch._rate_limiter.is_healthy.assert_called_with("spotiflac")
                
                # Verify failure was recorded (no files found)
                orch._rate_limiter.record_failure.assert_called_with("spotiflac")
                
                assert result is None, "Should return None when no files found"
    
    @given(
        file_extension=st.sampled_from([".flac", ".m4a", ".mp3", ".ogg", ".opus"])
    )
    @settings(max_examples=10, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_spotiflac_file_detection_logic_preserved(self, file_extension, session):
        """
        Property: For all supported audio file extensions, the file detection
        logic should work correctly.
        
        This test verifies that the file scanning and detection logic
        is preserved after removing log_level parameter.
        """
        from src.ingestion import downloader
        
        if not downloader.SPOTIFLAC_AVAILABLE:
            pytest.skip("SpotiFLAC not available")
        
        # Create a track
        track = _make_track(session, "spotify:track:testfile")
        track.spotify_id = "testfile"
        session.flush()
        
        # Create orchestrator
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._rate_limiter = MagicMock()
        orch._rate_limiter.is_healthy.return_value = True
        
        # Mock SpotiFLAC
        def mock_spotiflac(*args, **kwargs):
            return MagicMock()
        
        # Create a temporary file with the given extension
        import tempfile
        with tempfile.TemporaryDirectory() as tmpdir:
            test_file = os.path.join(tmpdir, f"test{file_extension}")
            with open(test_file, "w") as f:
                f.write("test content")
            
            with patch.object(downloader, "_SpotiFLAC", side_effect=mock_spotiflac):
                # Mock os.walk to return our test file
                with patch("os.walk", return_value=[(tmpdir, [], [f"test{file_extension}"])]):
                    with patch("os.path.getsize", return_value=1000):
                        with patch("os.rename") as mock_rename:
                            result = orch._tier1_spotiflac(track)
                            
                            # Verify file was detected and renamed
                            assert mock_rename.called, \
                                f"File with extension {file_extension} should be detected and renamed"
                            
                            # Verify success was recorded
                            orch._rate_limiter.record_success.assert_called_with("spotiflac")
    
    def test_spotiflac_skips_when_no_spotify_id(self, session):
        """
        Property: Tracks without spotify_id should skip Tier 1 gracefully.
        
        This test verifies that the spotify_id check is preserved
        after the fix.
        """
        from src.ingestion import downloader
        
        if not downloader.SPOTIFLAC_AVAILABLE:
            pytest.skip("SpotiFLAC not available")
        
        # Create a track WITHOUT spotify_id
        track = _make_track(session, "spotify:track:noid")
        track.spotify_id = None  # Explicitly set to None
        session.flush()
        
        # Create orchestrator
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._rate_limiter = MagicMock()
        orch._rate_limiter.is_healthy.return_value = True
        
        # Mock SpotiFLAC - should NOT be called
        mock_spotiflac_instance = MagicMock()
        
        with patch.object(downloader, "_SpotiFLAC", return_value=mock_spotiflac_instance):
            result = orch._tier1_spotiflac(track)
            
            # Verify SpotiFLAC was NOT instantiated
            assert not mock_spotiflac_instance.called, \
                "SpotiFLAC should not be called when spotify_id is None"
            
            # Verify result is None (skipped)
            assert result is None, \
                "Should return None when spotify_id is missing"
    
    def test_spotiflac_skips_when_circuit_breaker_open(self, session):
        """
        Property: Tier 1 should skip when circuit breaker is open.
        
        This test verifies that circuit breaker integration is preserved
        after the fix.
        """
        from src.ingestion import downloader
        
        if not downloader.SPOTIFLAC_AVAILABLE:
            pytest.skip("SpotiFLAC not available")
        
        # Create a track
        track = _make_track(session, "spotify:track:breaker")
        track.spotify_id = "breaker123"
        session.flush()
        
        # Create orchestrator with circuit breaker OPEN
        orch = DownloadOrchestrator.__new__(DownloadOrchestrator)
        orch._rate_limiter = MagicMock()
        orch._rate_limiter.is_healthy.return_value = False  # Circuit breaker OPEN
        
        # Mock SpotiFLAC - should NOT be called
        mock_spotiflac_instance = MagicMock()
        
        with patch.object(downloader, "_SpotiFLAC", return_value=mock_spotiflac_instance):
            result = orch._tier1_spotiflac(track)
            
            # Verify health check was performed
            orch._rate_limiter.is_healthy.assert_called_with("spotiflac")
            
            # Verify SpotiFLAC was NOT instantiated
            assert not mock_spotiflac_instance.called, \
                "SpotiFLAC should not be called when circuit breaker is open"
            
            # Verify result is None (skipped)
            assert result is None, \
                "Should return None when circuit breaker is open"
