"""
musicstream/ingestion/downloader.py — 5-tier download orchestrator

Implements the full tier chain for downloading tracks:
  Tier 1: SpotiFLAC (qobuz/tidal/amazon/deezer/youtube) — FLAC, 120s timeout
  Tier 2: yt-dlp + ytmusicapi (songs→videos→no filter) — MP3 320kbps, ±5s duration check
  Tier 3: spotdl — MP3 320kbps
  Tier 4: yt-dlp YouTube direct search (ytsearch12) — MP3 320kbps
  Tier 5: yt-dlp SoundCloud (scsearch8) — MP3 320kbps

After ≥9 failed attempts: status='failed', log [DOWNLOAD_FAIL] to errors.log.
MAX_CONCURRENT = 4 parallel workers via ThreadPoolExecutor.
"""

from __future__ import annotations

import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import yt_dlp  # type: ignore[import-untyped]
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from exceptions import DownloadError, SpotiFLACError
from models import DownloadAttempt, Track, TrackStatus
from rate_limiter import ServiceRateLimiter

logger = logging.getLogger(__name__)
errors_logger = logging.getLogger("errors")

# ── SpotiFLAC optional import ──────────────────────────────────────────────────

try:
    import spotiflac  # type: ignore[import-untyped]
    SPOTIFLAC_AVAILABLE = True
except ImportError:
    SPOTIFLAC_AVAILABLE = False
    logger.warning("SpotiFLAC not available; Tier 1 will be skipped")

# ── ytmusicapi optional import ─────────────────────────────────────────────────

try:
    from ytmusicapi import YTMusic  # type: ignore[import-untyped]
    YTMUSICAPI_AVAILABLE = True
except ImportError:
    YTMUSICAPI_AVAILABLE = False
    logger.warning("ytmusicapi not available; Tier 2 will be skipped")

# ── spotdl optional import ─────────────────────────────────────────────────────

try:
    from spotdl import Spotdl  # type: ignore[import-untyped]
    SPOTDL_AVAILABLE = True
except ImportError:
    SPOTDL_AVAILABLE = False
    logger.warning("spotdl not available; Tier 3 will be skipped")

# ── Constants ──────────────────────────────────────────────────────────────────

TEMP_DIR: str = os.environ.get("TEMP_DIR", "temp")
_SPOTIFLAC_SERVICES = ["qobuz", "tidal", "amazon", "deezer", "youtube"]
_SPOTIFLAC_TIMEOUT = 120  # seconds
_DURATION_TOLERANCE_S = 5  # ±5 seconds for duration validation
_GIVE_UP_THRESHOLD = 9     # ≥9 failed attempts → mark as failed


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DownloadOrchestrator:
    """
    5-tier download orchestrator.

    Downloads all pending tracks in parallel batches of MAX_CONCURRENT.
    Each track runs through the full tier chain; a single track failure
    never raises an exception that stops other downloads.
    """

    MAX_CONCURRENT = 4

    def __init__(self) -> None:
        self._rate_limiter = ServiceRateLimiter()
        os.makedirs(TEMP_DIR, exist_ok=True)

    # ── Public API ─────────────────────────────────────────────────────────────

    def download_pending(self, session: Session) -> tuple[int, int]:
        """
        Download all pending tracks using MAX_CONCURRENT parallel workers.

        Returns:
            (downloaded, failed) counts.
        """
        pending_tracks = (
            session.execute(
                select(Track).where(Track.status == TrackStatus.PENDING.value)
            )
            .scalars()
            .all()
        )

        if not pending_tracks:
            logger.info("No pending tracks to download.")
            return 0, 0

        logger.info("Starting download of %d pending tracks.", len(pending_tracks))

        downloaded = 0
        failed = 0

        # Use a fresh session per thread to avoid cross-thread session sharing
        from db import get_session  # local import to avoid circular deps

        def _download_one(track_id: int) -> bool:
            """Download a single track in its own session. Never raises."""
            try:
                with get_session() as thread_session:
                    track = thread_session.get(Track, track_id)
                    if track is None:
                        logger.warning("Track id=%d not found in DB; skipping.", track_id)
                        return False
                    return self.download_track(track, thread_session)
            except Exception as exc:
                logger.error(
                    "Unhandled exception downloading track id=%d: %s",
                    track_id,
                    exc,
                    exc_info=True,
                )
                return False

        track_ids = [t.id for t in pending_tracks]

        with ThreadPoolExecutor(max_workers=self.MAX_CONCURRENT) as executor:
            futures = {executor.submit(_download_one, tid): tid for tid in track_ids}
            for future in as_completed(futures):
                tid = futures[future]
                try:
                    success = future.result()
                except Exception as exc:
                    logger.error(
                        "Future for track id=%d raised unexpectedly: %s", tid, exc
                    )
                    success = False

                if success:
                    downloaded += 1
                else:
                    failed += 1

        logger.info(
            "Download batch complete: downloaded=%d failed=%d", downloaded, failed
        )
        return downloaded, failed

    def download_track(self, track: Track, session: Session) -> bool:
        """
        Run the full 5-tier chain for a single track.

        Records every attempt in download_attempts. On success, updates
        track.status and track.download_method. On exhaustion, marks
        status='failed' if ≥9 failed attempts.

        Returns:
            True if the track was successfully downloaded, False otherwise.
        """
        # Mark as downloading
        track.status = TrackStatus.DOWNLOADING.value
        session.flush()

        tiers = [
            ("tier1_spotiflac",    self._tier1_spotiflac),
            ("tier2_ytdlp_ytm",    self._tier2_ytdlp_ytm),
            ("tier3_spotdl",       self._tier3_spotdl),
            ("tier4_ytdlp_youtube", self._tier4_ytdlp_youtube),
            ("tier5_ytdlp_soundcloud", self._tier5_ytdlp_soundcloud),
        ]

        for method_name, tier_fn in tiers:
            try:
                path = tier_fn(track)
                if path:
                    self._record_attempt(
                        session, track.id, method_name, error=None, success=True
                    )
                    # Determine the canonical download_method label
                    download_method = self._resolve_method_label(method_name, path)
                    track.download_method = download_method
                    track.status = TrackStatus.DOWNLOADED.value
                    session.flush()
                    logger.info(
                        "Downloaded track id=%d via %s: %s",
                        track.id,
                        download_method,
                        path,
                    )
                    return True
                else:
                    # Tier returned None without raising — treat as soft failure
                    self._record_attempt(
                        session,
                        track.id,
                        method_name,
                        error="tier returned None",
                        success=False,
                    )
            except Exception as exc:
                self._record_attempt(
                    session, track.id, method_name, error=str(exc), success=False
                )
                logger.warning(
                    "Tier %s failed for track id=%d: %s",
                    method_name,
                    track.id,
                    exc,
                )

        # All tiers exhausted
        if self._should_give_up(session, track.id):
            track.status = TrackStatus.FAILED.value
            session.flush()
            errors_logger.error(
                "[DOWNLOAD_FAIL] %s | %s | attempts=%d | last_error=all tiers exhausted",
                track.title,
                track.artist,
                _GIVE_UP_THRESHOLD,
            )
            logger.error(
                "[DOWNLOAD_FAIL] track id=%d '%s' by '%s' — marked as failed.",
                track.id,
                track.title,
                track.artist,
            )
        else:
            # Leave as pending for the next run
            track.status = TrackStatus.PENDING.value
            session.flush()
            logger.info(
                "Track id=%d '%s' — all tiers failed this run; will retry next run.",
                track.id,
                track.title,
            )

        return False

    # ── Tier 1: SpotiFLAC ─────────────────────────────────────────────────────

    def _tier1_spotiflac(self, track: Track) -> Optional[str]:
        """
        Attempt download via SpotiFLAC with services=['qobuz','tidal','amazon','deezer','youtube'].
        Returns temp file path (FLAC) or None. Timeout: 120s.
        Gracefully skipped if SpotiFLAC is not installed.
        """
        if not SPOTIFLAC_AVAILABLE:
            return None

        if not self._rate_limiter.is_healthy("spotiflac"):
            logger.warning("SpotiFLAC circuit breaker open; skipping Tier 1.")
            return None

        out_path = os.path.join(TEMP_DIR, f"{uuid.uuid4()}.flac")

        try:
            import signal

            def _timeout_handler(signum, frame):
                raise TimeoutError(f"SpotiFLAC timed out after {_SPOTIFLAC_TIMEOUT}s")

            # Use signal-based timeout on Unix; on Windows fall back to thread approach
            use_signal = hasattr(signal, "SIGALRM")

            if use_signal:
                old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
                signal.alarm(_SPOTIFLAC_TIMEOUT)

            try:
                result = spotiflac.download(
                    track.spotify_uri,
                    services=_SPOTIFLAC_SERVICES,
                    output=out_path,
                )
            finally:
                if use_signal:
                    signal.alarm(0)
                    signal.signal(signal.SIGALRM, old_handler)

            if result and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                self._rate_limiter.record_success("spotiflac")
                # Determine which service succeeded for the method label
                service_used = getattr(result, "service", None) or "unknown"
                # Store service on the path so _resolve_method_label can use it
                # We encode it in the filename convention: {uuid}_{service}.flac
                labeled_path = os.path.join(
                    TEMP_DIR, f"{uuid.uuid4()}_{service_used}.flac"
                )
                os.rename(out_path, labeled_path)
                return labeled_path
            else:
                self._rate_limiter.record_failure("spotiflac")
                return None

        except TimeoutError as exc:
            self._rate_limiter.record_failure("spotiflac")
            raise SpotiFLACError(f"SpotiFLAC timeout: {exc}") from exc
        except Exception as exc:
            self._rate_limiter.record_failure("spotiflac")
            raise SpotiFLACError(f"SpotiFLAC error: {exc}") from exc

    # ── Tier 2: yt-dlp + ytmusicapi ───────────────────────────────────────────

    def _tier2_ytdlp_ytm(self, track: Track) -> Optional[str]:
        """
        Search via ytmusicapi (songs → videos → no filter), download with yt-dlp
        bestaudio → FFmpeg → MP3 320kbps. Validates duration ±5s.
        Returns temp file path or None.
        """
        if not YTMUSICAPI_AVAILABLE:
            return None

        if not self._rate_limiter.is_healthy("ytmusicapi"):
            logger.warning("ytmusicapi circuit breaker open; skipping Tier 2.")
            return None

        query = f"{track.title} {track.artist}"
        video_id: Optional[str] = None

        # Search order: songs → videos → no filter
        search_filters = ["songs", "videos", None]
        for search_filter in search_filters:
            try:
                ytm = YTMusic()
                kwargs = {"query": query, "limit": 5}
                if search_filter is not None:
                    kwargs["filter"] = search_filter
                results = ytm.search(**kwargs)
                for result in results:
                    vid = result.get("videoId")
                    if vid:
                        video_id = vid
                        break
                if video_id:
                    break
            except Exception as exc:
                self._rate_limiter.record_failure("ytmusicapi")
                logger.warning("ytmusicapi search (filter=%s) failed: %s", search_filter, exc)
                continue

        if not video_id:
            return None

        self._rate_limiter.record_success("ytmusicapi")

        if not self._rate_limiter.is_healthy("youtube"):
            logger.warning("YouTube circuit breaker open; skipping Tier 2 download.")
            return None

        out_stem = os.path.join(TEMP_DIR, str(uuid.uuid4()))
        out_path = out_stem + ".mp3"

        ydl_opts = self._build_mp3_opts(out_stem)
        url = f"https://www.youtube.com/watch?v={video_id}"

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                info = ydl.extract_info(url, download=True)

            # Find the downloaded file
            downloaded = self._find_output_file(out_stem)
            if not downloaded or not os.path.exists(downloaded):
                self._rate_limiter.record_failure("youtube")
                return None

            # Duration validation ±5s
            if track.duration_ms is not None and info is not None:
                expected_s = track.duration_ms / 1000.0
                got_s = info.get("duration") or 0
                delta = abs(got_s - expected_s)
                if delta > _DURATION_TOLERANCE_S:
                    errors_logger.warning(
                        "[DURATION_MISMATCH] %s | %s | expected=%.0fms | got=%.0fs | delta=%.1fs | tier=2",
                        track.title,
                        track.artist,
                        track.duration_ms,
                        got_s,
                        delta,
                    )
                    os.remove(downloaded)
                    self._rate_limiter.record_failure("youtube")
                    return None

            self._rate_limiter.record_success("youtube")
            return downloaded

        except Exception as exc:
            self._rate_limiter.record_failure("youtube")
            raise DownloadError(f"Tier 2 yt-dlp download failed: {exc}") from exc

    # ── Tier 3: spotdl ────────────────────────────────────────────────────────

    def _tier3_spotdl(self, track: Track) -> Optional[str]:
        """
        Download via spotdl using the Spotify URI. Output: MP3 320kbps to temp/.
        Returns temp file path or None.
        """
        if not SPOTDL_AVAILABLE:
            return None

        if not self._rate_limiter.is_healthy("spotdl"):
            logger.warning("spotdl circuit breaker open; skipping Tier 3.")
            return None

        try:
            spotdl_client = Spotdl(
                client_id=os.environ.get("SPOTIFY_CLIENT_ID", ""),
                client_secret=os.environ.get("SPOTIFY_CLIENT_SECRET", ""),
            )

            # spotdl downloads to the current directory by default; redirect to temp/
            original_dir = os.getcwd()
            os.chdir(TEMP_DIR)
            try:
                songs, _ = spotdl_client.search([track.spotify_uri])
                if not songs:
                    self._rate_limiter.record_failure("spotdl")
                    return None

                paths = spotdl_client.download_songs(songs)
            finally:
                os.chdir(original_dir)

            if paths:
                downloaded_path = paths[0] if isinstance(paths[0], str) else str(paths[0])
                # Ensure the path is absolute / relative to TEMP_DIR
                if not os.path.isabs(downloaded_path):
                    downloaded_path = os.path.join(TEMP_DIR, downloaded_path)
                if os.path.exists(downloaded_path) and os.path.getsize(downloaded_path) > 0:
                    self._rate_limiter.record_success("spotdl")
                    return downloaded_path

            self._rate_limiter.record_failure("spotdl")
            return None

        except Exception as exc:
            self._rate_limiter.record_failure("spotdl")
            raise DownloadError(f"Tier 3 spotdl failed: {exc}") from exc

    # ── Tier 4: yt-dlp YouTube direct search ─────────────────────────────────

    def _tier4_ytdlp_youtube(self, track: Track) -> Optional[str]:
        """
        Search YouTube with ytsearch12 using two query variants:
          - "{title} {artist} audio"
          - "{title} {artist} official audio"
        Returns temp file path (MP3 320kbps) or None.
        """
        if not self._rate_limiter.is_healthy("youtube"):
            logger.warning("YouTube circuit breaker open; skipping Tier 4.")
            return None

        queries = [
            f"ytsearch12:{track.title} {track.artist} audio",
            f"ytsearch12:{track.title} {track.artist} official audio",
        ]

        for query in queries:
            out_stem = os.path.join(TEMP_DIR, str(uuid.uuid4()))
            ydl_opts = self._build_mp3_opts(out_stem)

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([query])

                downloaded = self._find_output_file(out_stem)
                if downloaded and os.path.exists(downloaded) and os.path.getsize(downloaded) > 0:
                    self._rate_limiter.record_success("youtube")
                    return downloaded

            except Exception as exc:
                self._rate_limiter.record_failure("youtube")
                logger.warning("Tier 4 query '%s' failed: %s", query, exc)
                continue

        return None

    # ── Tier 5: yt-dlp SoundCloud ─────────────────────────────────────────────

    def _tier5_ytdlp_soundcloud(self, track: Track) -> Optional[str]:
        """
        Search SoundCloud with scsearch8 using "{title} {artist}".
        Returns temp file path (MP3 320kbps) or None.
        """
        if not self._rate_limiter.is_healthy("youtube"):
            logger.warning("YouTube/yt-dlp circuit breaker open; skipping Tier 5.")
            return None

        query = f"scsearch8:{track.title} {track.artist}"
        out_stem = os.path.join(TEMP_DIR, str(uuid.uuid4()))
        ydl_opts = self._build_mp3_opts(out_stem)

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([query])

            downloaded = self._find_output_file(out_stem)
            if downloaded and os.path.exists(downloaded) and os.path.getsize(downloaded) > 0:
                self._rate_limiter.record_success("youtube")
                return downloaded

            return None

        except Exception as exc:
            self._rate_limiter.record_failure("youtube")
            raise DownloadError(f"Tier 5 SoundCloud failed: {exc}") from exc

    # ── Attempt recording ──────────────────────────────────────────────────────

    def _record_attempt(
        self,
        session: Session,
        track_id: int,
        method: str,
        error: Optional[str],
        success: bool,
    ) -> None:
        """Write a DownloadAttempt row to the download_attempts table."""
        attempt = DownloadAttempt(
            track_id=track_id,
            attempted_at=_utcnow(),
            method=method,
            error=error,
            success=success,
        )
        session.add(attempt)
        session.flush()

    # ── Give-up logic ──────────────────────────────────────────────────────────

    def _should_give_up(self, session: Session, track_id: int) -> bool:
        """
        Returns True if the track has ≥9 failed attempts recorded.
        (3 complete tier-chain runs × 5 tiers = 15 max, but threshold is 9.)
        """
        failed_count = session.execute(
            select(func.count(DownloadAttempt.id)).where(
                DownloadAttempt.track_id == track_id,
                DownloadAttempt.success == False,  # noqa: E712
            )
        ).scalar_one()

        return failed_count >= _GIVE_UP_THRESHOLD

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _build_mp3_opts(self, out_stem: str) -> dict:
        """
        Build yt-dlp options for bestaudio → FFmpeg → MP3 320kbps output.
        Output template uses the stem; the final file will be {stem}.mp3.
        """
        opts: dict = {
            "format": "bestaudio/best",
            "outtmpl": out_stem + ".%(ext)s",
            "postprocessors": [
                {
                    "key": "FFmpegExtractAudio",
                    "preferredcodec": "mp3",
                    "preferredquality": "320",
                }
            ],
            "quiet": True,
            "no_warnings": True,
            "retries": 3,
            "fragment_retries": 3,
            "skip_unavailable_fragments": True,
            "noplaylist": True,
        }

        cookies_path = "cookies.txt"
        if os.path.exists(cookies_path) and os.path.getsize(cookies_path) > 0:
            opts["cookiefile"] = cookies_path

        return opts

    def _find_output_file(self, out_stem: str) -> Optional[str]:
        """Find the downloaded file matching the stem (any audio extension)."""
        for ext in ("mp3", "m4a", "opus", "webm", "flac", "ogg"):
            candidate = out_stem + f".{ext}"
            if os.path.exists(candidate):
                return candidate
        return None

    @staticmethod
    def _resolve_method_label(tier_name: str, file_path: str) -> str:
        """
        Map internal tier name + file path to the canonical download_method label.

        Labels per spec:
          spotiflac_qobuz | spotiflac_tidal | spotiflac_amazon |
          spotiflac_deezer | spotiflac_youtube |
          ytdlp_ytm | spotdl | ytdlp_yt | ytdlp_soundcloud
        """
        if tier_name == "tier1_spotiflac":
            # Service name was encoded in the filename: {uuid}_{service}.flac
            basename = os.path.basename(file_path)
            name_no_ext = os.path.splitext(basename)[0]
            parts = name_no_ext.split("_", 1)
            service = parts[1] if len(parts) == 2 else "unknown"
            return f"spotiflac_{service}"
        elif tier_name == "tier2_ytdlp_ytm":
            return "ytdlp_ytm"
        elif tier_name == "tier3_spotdl":
            return "spotdl"
        elif tier_name == "tier4_ytdlp_youtube":
            return "ytdlp_yt"
        elif tier_name == "tier5_ytdlp_soundcloud":
            return "ytdlp_soundcloud"
        else:
            return tier_name
