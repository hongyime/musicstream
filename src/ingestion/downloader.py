"""
musicstream/ingestion/downloader.py — 5-tier download orchestrator

Implements the full tier chain for downloading tracks:
  Tier 1: SpotiFLAC (qobuz/tidal/amazon/deezer) — FLAC lossless
  Tier 2: yt-dlp + ytmusicapi (songs→videos→no filter) — MP3 320kbps, ±5s duration check
  Tier 3: spotdl Python API — MP3 320kbps, requires Spotify credentials
  Tier 4: yt-dlp YouTube direct search (ytsearch12) — MP3 320kbps
  Tier 5: yt-dlp SoundCloud (scsearch8) — MP3 320kbps, uses separate "soundcloud" circuit breaker

After ≥25 failed attempts: status='failed', log [DOWNLOAD_FAIL] to errors.log.
MAX_CONCURRENT = 4 parallel workers via ThreadPoolExecutor.
"""

from __future__ import annotations

import asyncio
import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Any, Optional

import yt_dlp  # type: ignore[import-untyped]
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.exceptions import DownloadError, OrganiserError, TaggingError
from src.models import DownloadAttempt, Track, TrackStatus
from src.rate_limiter import ServiceRateLimiter, ServiceThrottle

logger = logging.getLogger(__name__)
errors_logger = logging.getLogger("errors")

# ── SpotiFLAC optional import ──────────────────────────────────────────────────

try:
    from spotiflac import SpotiFLAC as _SpotiFLAC
    SPOTIFLAC_AVAILABLE = True
except ImportError:
    try:
        from SpotiFLAC import SpotiFLAC as _SpotiFLAC
        SPOTIFLAC_AVAILABLE = True
    except ImportError:
        SPOTIFLAC_AVAILABLE = False
        logger.warning("SpotiFLAC not available (ImportError); Tier 1 skipped")

if SPOTIFLAC_AVAILABLE:
    logger.info("SpotiFLAC available — Tier 1 active")

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

# ── Constants ───────────────────────────────────────────────────────────────────

TEMP_DIR: str = os.environ.get("TEMP_DIR", "temp")
_DURATION_TOLERANCE_S = 5  # ±5 seconds for duration validation
_GIVE_UP_THRESHOLD = 25    # ~5 complete tier-chain runs before giving up

# Worker concurrency — configurable via environment variable
# Default: 4 workers, can be increased to 6 or 8 for faster downloads
# Note: Increasing significantly may trigger API rate limits
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_WORKERS", "4"))
logger.info("Worker concurrency set to: MAX_CONCURRENT=%d", MAX_CONCURRENT)

# spotdl serialisation — Spotify's API rate-limits quickly under concurrent calls.
# One active spotdl download at a time prevents 4 workers from hitting the limit together.
_SPOTDL_SEMAPHORE = threading.Semaphore(1)

# spotdl process-wide singleton — SpotifyClient is a class-level singleton internally;
# creating a second Spotdl instance in the same process raises "already initialized".
_SPOTDL_CLIENT: Optional[Any] = None
_SPOTDL_CLIENT_LOCK = threading.Lock()

# SpotiFLAC serialisation — deezmate and monochrome.tf return 429 under concurrent load.
# Two slots preserves some parallelism while halving the request rate.
_SPOTIFLAC_SEMAPHORE = threading.Semaphore(2)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DownloadOrchestrator:
    """
    5-tier download orchestrator.

    Downloads all pending tracks in parallel batches of MAX_CONCURRENT.
    Each track runs through the full tier chain; a single track failure
    never raises an exception that stops other downloads.
    """

    def __init__(self) -> None:
        # Scale circuit breaker threshold based on concurrency
        # This prevents 4 concurrent workers from tripping a low threshold
        # when the same API fails for all workers simultaneously.
        # Threshold=MAX_CONCURRENT*5 allows for burst failures while catching
        # persistent issues. Cooldown=300s allows breaker to recover within
        # the same batch run.
        circuit_threshold = MAX_CONCURRENT * 5
        self._rate_limiter = ServiceRateLimiter(
            circuit_breaker_threshold=circuit_threshold,
            circuit_breaker_cooldown=300,
        )
        self._throttle = ServiceThrottle()
        os.makedirs(TEMP_DIR, exist_ok=True)

        # Lazy-init tagger and organiser from env vars.
        # Imported here to avoid circular imports at module level.
        from src.ingestion.tagger import MetadataTagger
        from src.ingestion.organiser import FileOrganiser

        self._tagger = MetadataTagger(
            acoustid_api_key=os.environ.get("ACOUSTID_API_KEY", ""),
        )
        # MEDIA_DIR is the container-internal mount point (always /media).
        # EXTERNAL_MEDIA_DRIVE is the HOST path — wrong inside the container.
        media_drive = os.environ.get("MEDIA_DIR") or os.environ.get("EXTERNAL_MEDIA_DRIVE", "/media")
        plex_url = os.environ.get("PLEX_URL", "http://localhost:32400")
        plex_token = os.environ.get("PLEX_TOKEN", "")
        plex_section_id = os.environ.get("PLEX_LIBRARY_SECTION_ID", "")
        self._organiser = FileOrganiser(
            media_drive=media_drive,
            plex_url=plex_url,
            plex_token=plex_token,
            plex_section_id=plex_section_id,
        )

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
        from src.db import get_session  # local import to avoid circular deps

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

        # Process in batches with delays between batches to avoid rate limits
        batch_size = MAX_CONCURRENT
        total_batches = (len(track_ids) + batch_size - 1) // batch_size

        for batch_num in range(total_batches):
            start_idx = batch_num * batch_size
            end_idx = min(start_idx + batch_size, len(track_ids))
            batch_track_ids = track_ids[start_idx:end_idx]

            logger.info(
                "Processing batch %d/%d (%d tracks)",
                batch_num + 1,
                total_batches,
                len(batch_track_ids),
            )

            with ThreadPoolExecutor(max_workers=MAX_CONCURRENT) as executor:
                futures = {executor.submit(_download_one, tid): tid for tid in batch_track_ids}
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

            # Add delay between batches (except last) to prevent rate limiting
            # Reduced to 5 seconds for 12 workers - balances speed + API safety
            if batch_num < total_batches - 1:
                delay_seconds = 5 if MAX_CONCURRENT >= 10 else 10  # 5s for 10+ workers, 10s for fewer
                logger.debug("Pausing %d seconds before next batch...", delay_seconds)
                time.sleep(delay_seconds)

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
            # Ordered: highest quality → most reliable fallback
            ("tier1_spotiflac",        self._tier1_spotiflac),         # FLAC lossless
            ("tier2_ytdlp_ytm",        self._tier2_ytdlp_ytm),         # MP3 320, best YT matching
            ("tier3_spotdl",           self._tier3_spotdl),            # MP3 320, needs client_secret
            ("tier4_ytdlp_youtube",    self._tier4_ytdlp_youtube),     # MP3 320, reliable direct search
            ("tier5_ytdlp_soundcloud", self._tier5_ytdlp_soundcloud),  # MP3 320, last resort
        ]

        for method_name, tier_fn in tiers:
            try:
                path = tier_fn(track)
                if path:
                    self._record_attempt(
                        session, track.id, method_name, error=None, success=True
                    )
                    download_method = self._resolve_method_label(method_name, path)
                    track.download_method = download_method
                    session.flush()

                    # ── Tag the file (non-fatal: bad tags ≠ bad download) ──────
                    try:
                        self._tagger.tag_file(path, track, session)
                    except TaggingError as tag_exc:
                        logger.warning(
                            "Tagging failed for track %d ('%s'): %s — proceeding without full tags",
                            track.id, track.title, tag_exc,
                        )
                    except Exception as tag_exc:
                        logger.warning(
                            "Unexpected tagging error for track %d: %s",
                            track.id, tag_exc,
                        )

                    # ── Move file into Plex library (fatal: no file = no point) ─
                    try:
                        final_path = self._organiser.organise(path, track, session)
                        logger.info(
                            "Track %d ('%s') delivered via %s → %s",
                            track.id, track.title, download_method, final_path,
                        )
                        return True
                    except OrganiserError as org_exc:
                        logger.error(
                            "Organiser failed for track %d ('%s'): %s",
                            track.id, track.title, org_exc,
                        )
                        track.status = TrackStatus.FAILED_VALIDATION.value
                        session.flush()
                        return False

                else:
                    # Tier returned None without raising — soft failure, try next tier
                    logger.info(
                        "Tier %s → no result for track %d ('%s'); trying next tier",
                        method_name, track.id, track.title,
                    )
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
        Attempt FLAC download via SpotiFLAC (qobuz → tidal → amazon → deezer).
        Returns the downloaded file path, or None on failure.

        Rate-limit handling:
          - 429 / "Too Many Requests": soft-fail with retry (up to 3 attempts with exponential backoff)
          - DNS / connection errors: CB failure — retrying immediately won't help.

        Concurrency: _SPOTIFLAC_SEMAPHORE(2) caps parallel calls so backends
        are not hammered by all 4 workers simultaneously.

        Retry logic for 429 errors:
          - First attempt: no delay
          - Second attempt: 15 seconds wait
          - Third attempt: 30 seconds wait
        """
        if not SPOTIFLAC_AVAILABLE:
            logger.warning("Tier 1 skipped for track %d ('%s'): SpotiFLAC not installed", track.id, track.title)
            return None

        if not self._rate_limiter.is_healthy("spotiflac"):
            logger.warning("SpotiFLAC circuit breaker open; skipping Tier 1 for track %d", track.id)
            return None

        if not track.spotify_id:
            logger.warning("Tier 1 skipped for track %d ('%s'): no spotify_id", track.id, track.title)
            return None

        spotify_url = f"https://open.spotify.com/track/{track.spotify_id}"

        # Retry logic for 429 rate limit errors
        max_retries = 3
        retry_delays = [0, 15, 30]  # seconds: first try, then 15s, then 30s

        for attempt in range(max_retries):
            if not self._throttle.wait("spotiflac"):
                logger.info("Throttle skip: spotiflac track %d — will retry next run", track.id)
                return None

            if not _SPOTIFLAC_SEMAPHORE.acquire(timeout=30):
                logger.warning("SpotiFLAC semaphore timeout for track %d; skipping this run.", track.id)
                return None

            out_dir = os.path.join(TEMP_DIR, f"spotiflac_{uuid.uuid4().hex}")
            os.makedirs(out_dir, exist_ok=True)

            try:
                # Wait before retry (not on first attempt)
                if attempt > 0:
                    delay = retry_delays[attempt]
                    logger.info(
                        "SpotiFLAC retry %d/%d for track %d ('%s'): waiting %ds due to rate limit",
                        attempt + 1, max_retries, track.id, track.title, delay,
                    )
                    time.sleep(delay)

                # Run in a thread so we can impose a hard timeout — SpotiFLAC can
                # hang indefinitely on a DNS failure before raising.
                import concurrent.futures as _cf
                
                # Prepare Qobuz credentials if available
                qobuz_email = os.environ.get("QOBUZ_EMAIL", "")
                qobuz_password_md5 = os.environ.get("QOBUZ_PASSWORD_MD5", "")
                
                with _cf.ThreadPoolExecutor(max_workers=1) as ex:
                    spotiflac_kwargs = {
                        "url": spotify_url,
                        "output_dir": out_dir,
                        "services": ["qobuz", "tidal", "amazon", "deezer"],
                    }
                    # Only add Qobuz credentials if both are provided
                    if qobuz_email and qobuz_password_md5:
                        spotiflac_kwargs["qobuz_email"] = qobuz_email
                        spotiflac_kwargs["qobuz_password_md5"] = qobuz_password_md5
                        logger.debug("Using Qobuz authentication for track %d", track.id)
                    
                    fut = ex.submit(_SpotiFLAC, **spotiflac_kwargs)
                    try:
                        fut.result(timeout=150)
                    except _cf.TimeoutError:
                        logger.warning("SpotiFLAC timed out for track %d ('%s')", track.id, track.title)
                        self._rate_limiter.record_failure("spotiflac")
                        _SPOTIFLAC_SEMAPHORE.release()
                        continue  # Try next retry

            except Exception as exc:
                # Walk the full exception chain — SpotiFLAC wraps backend 429s in
                # generic messages ("Deezer download failed"), so str(exc) alone misses them.
                if self._exc_contains_rate_limit(exc):
                    logger.warning(
                        "SpotiFLAC rate-limited for track %d ('%s') (attempt %d/%d) — backend overloaded",
                        track.id, track.title, attempt + 1, max_retries,
                    )
                    self._throttle.on_rate_limit("spotiflac")
                    _SPOTIFLAC_SEMAPHORE.release()

                    # If we haven't exhausted retries, continue to the next attempt
                    if attempt < max_retries - 1:
                        continue
                    else:
                        logger.warning(
                            "SpotiFLAC exhausted all retries for track %d ('%s') — will retry next run",
                            track.id, track.title,
                        )
                        return None

                # "Failed all services" means every backend had a transient error
                # (DNS/-3, 429 wrapped, etc.) — SpotiFLAC itself is healthy.
                msg = str(exc).lower()
                if "failed all services" in msg or "all services failed" in msg:
                    logger.warning(
                        "SpotiFLAC all-services transient failure for track %d ('%s') (attempt %d/%d): %s",
                        track.id, track.title, attempt + 1, max_retries, exc,
                    )
                    _SPOTIFLAC_SEMAPHORE.release()

                    # Check if this might be a rate limit in the exception chain
                    if self._exc_contains_rate_limit(exc):
                        if attempt < max_retries - 1:
                            continue
                        else:
                            return None
                    return None

                logger.warning("SpotiFLAC failed for track %d ('%s'): %s", track.id, track.title, exc)
                self._rate_limiter.record_failure("spotiflac")
                _SPOTIFLAC_SEMAPHORE.release()
                return None

            # Check for downloaded files
            for root, _, files in os.walk(out_dir):
                for fname in files:
                    if fname.endswith((".flac", ".m4a", ".mp3", ".ogg", ".opus")):
                        found = os.path.join(root, fname)
                        if os.path.getsize(found) > 0:
                            ext = os.path.splitext(fname)[1]
                            dest = os.path.join(out_dir, f"{uuid.uuid4().hex}_spotiflac{ext}")
                            os.rename(found, dest)
                            self._rate_limiter.record_success("spotiflac")
                            self._throttle.on_success("spotiflac")
                            logger.info("SpotiFLAC: track %d downloaded → %s (attempt %d/%d)", track.id, os.path.basename(dest), attempt + 1, max_retries)
                            _SPOTIFLAC_SEMAPHORE.release()
                            return dest

            logger.warning("SpotiFLAC: no file produced for track %d ('%s') (attempt %d/%d)", track.id, track.title, attempt + 1, max_retries)
            _SPOTIFLAC_SEMAPHORE.release()

        # All retries exhausted
        logger.warning("SpotiFLAC: exhausted all retries for track %d ('%s')", track.id, track.title)
        self._rate_limiter.record_failure("spotiflac")
        return None

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

        # ytmusicapi returns YouTube Music video IDs.  Without cookies those
        # are all Premium-gated and return "Requested format is not available".
        # Skip the tier entirely when cookies.txt is absent or empty to avoid
        # burning 30+ seconds on guaranteed failures.
        cookies_ok = os.path.exists("cookies.txt") and os.path.getsize("cookies.txt") > 0
        if not cookies_ok:
            logger.info(
                "Tier 2 skipped for track %d: cookies.txt empty — ytmusicapi IDs require YouTube auth. "
                "Export browser cookies to cookies.txt to enable this tier.",
                track.id,
            )
            return None

        query = f"{track.title} {track.artist}"

        # Collect up to 4 candidates per filtered search (songs → videos only).
        # Skipping the no-filter pass avoids hundreds of album/artist results
        # that have no videoId and would still be YouTube Music Premium content.
        candidates: list[str] = []
        for search_filter in ("songs", "videos"):
            try:
                ytm = YTMusic()
                results = ytm.search(query=query, filter=search_filter, limit=4)
                for result in results:
                    vid = result.get("videoId")
                    if vid and vid not in candidates:
                        candidates.append(vid)
            except Exception as exc:
                self._rate_limiter.record_failure("ytmusicapi")
                logger.warning("ytmusicapi search (filter=%s) failed: %s", search_filter, exc)

        if not candidates:
            return None

        self._rate_limiter.record_success("ytmusicapi")

        if not self._rate_limiter.is_healthy("youtube"):
            logger.warning("YouTube circuit breaker open; skipping Tier 2 download.")
            return None

        for video_id in candidates:
            if not self._throttle.wait("youtube"):
                logger.info("Throttle skip: youtube tier 2 track %d — will retry next run", track.id)
                return None

            out_stem = os.path.join(TEMP_DIR, str(uuid.uuid4()))
            ydl_opts = self._build_mp3_opts(out_stem)
            url = f"https://www.youtube.com/watch?v={video_id}"

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    info = ydl.extract_info(url, download=True)

                downloaded = self._find_output_file(out_stem)
                if not downloaded or not os.path.exists(downloaded):
                    self._rate_limiter.record_failure("youtube")
                    continue

                # Duration validation ±5s
                if track.duration_ms is not None and info is not None:
                    expected_s = track.duration_ms / 1000.0
                    got_s = info.get("duration") or 0
                    delta = abs(got_s - expected_s)
                    if delta > _DURATION_TOLERANCE_S:
                        errors_logger.warning(
                            "[DURATION_MISMATCH] %s | %s | expected=%.0fms | got=%.0fs | delta=%.1fs | tier=2 vid=%s",
                            track.title, track.artist,
                            track.duration_ms, got_s, delta, video_id,
                        )
                        os.remove(downloaded)
                        continue

                self._rate_limiter.record_success("youtube")
                self._throttle.on_success("youtube")
                return downloaded

            except Exception as exc:
                if self._is_youtube_session_rate_limited(exc):
                    self._rate_limiter.force_open("youtube", "session rate-limited")
                    self._throttle.on_rate_limit("youtube")
                    logger.warning(
                        "YouTube session rate-limited for track %d; circuit breaker opened",
                        track.id,
                    )
                    return None
                if self._is_content_error(exc):
                    logger.info(
                        "Tier 2 video %s content-restricted for track %d — trying next candidate: %s",
                        video_id, track.id, exc,
                    )
                else:
                    self._rate_limiter.record_failure("youtube")
                    raise DownloadError(f"Tier 2 yt-dlp download failed: {exc}") from exc

        return None

    # ── Tier 3: spotdl ────────────────────────────────────────────────────────

    def _tier3_spotdl(self, track: Track) -> Optional[str]:
        """
        Download via spotdl Python API using the Spotify URI. Output: MP3 320kbps to temp/.
        Returns temp file path or None.
        Requires SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET env vars.
        """
        if not SPOTDL_AVAILABLE:
            return None

        if not self._rate_limiter.is_healthy("spotdl"):
            logger.warning("spotdl circuit breaker open; skipping Tier 3.")
            return None

        client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
        client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            logger.warning("SPOTIFY_CLIENT_ID/SECRET not set; skipping Tier 3 spotdl.")
            return None

        if not self._throttle.wait("spotdl"):
            logger.info("Throttle skip: spotdl track %d — will retry next run", track.id)
            return None

        # Serialise — Spotify's API rate-limits quickly under concurrent load.
        if not _SPOTDL_SEMAPHORE.acquire(timeout=60):
            logger.warning("spotdl semaphore timeout for track %d; skipping Tier 3 this run.", track.id)
            return None

        try:
            client = self._get_spotdl_client(client_id, client_secret)
            if client is None:
                return None

            songs = client.search([track.spotify_uri])
            if not songs:
                logger.warning("spotdl: no results for track %d ('%s')", track.id, track.title)
                self._rate_limiter.record_failure("spotdl")
                return None

            _song, path = client.download(songs[0])
            if path is None or not path.exists():
                self._rate_limiter.record_failure("spotdl")
                return None

            self._rate_limiter.record_success("spotdl")
            self._throttle.on_success("spotdl")
            return str(path)

        except Exception as exc:
            msg = str(exc)
            if "rate" in msg.lower() or "limit" in msg.lower() or "retry will occur" in msg.lower():
                logger.warning(
                    "spotdl rate-limited for track %d ('%s'): %s — skipping this run",
                    track.id, track.title, msg.splitlines()[0],
                )
                self._throttle.on_rate_limit("spotdl")
                return None
            self._rate_limiter.record_failure("spotdl")
            raise DownloadError(f"Tier 3 spotdl failed: {exc}") from exc
        finally:
            _SPOTDL_SEMAPHORE.release()

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
            if not self._throttle.wait("youtube"):
                logger.info("Throttle skip: youtube tier 4 track %d — will retry next run", track.id)
                return None

            out_stem = os.path.join(TEMP_DIR, str(uuid.uuid4()))
            ydl_opts = self._build_mp3_opts(out_stem)
            # For search queries, disable noplaylist so yt-dlp walks the result
            # list.  ignoreerrors skips restricted videos silently; max_downloads
            # stops after the first successful download.
            ydl_opts["noplaylist"] = False
            ydl_opts["ignoreerrors"] = True
            ydl_opts["max_downloads"] = 1

            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([query])
            except yt_dlp.utils.MaxDownloadsReached:
                pass  # expected: yt-dlp raises this after max_downloads=1 succeeds
            except Exception as exc:
                if self._is_youtube_session_rate_limited(exc):
                    self._rate_limiter.force_open("youtube", "session rate-limited")
                    self._throttle.on_rate_limit("youtube")
                    logger.warning("YouTube session rate-limited; circuit breaker opened, stopping Tier 4")
                    return None
                if not self._is_content_error(exc):
                    self._rate_limiter.record_failure("youtube")
                logger.warning("Tier 4 query '%s' failed: %s", query, exc)
                continue

            downloaded = self._find_output_file(out_stem)
            if downloaded and os.path.exists(downloaded) and os.path.getsize(downloaded) > 0:
                self._rate_limiter.record_success("youtube")
                self._throttle.on_success("youtube")
                return downloaded

        return None

    # ── Tier 5: yt-dlp SoundCloud ─────────────────────────────────────────────

    def _tier5_ytdlp_soundcloud(self, track: Track) -> Optional[str]:
        """
        Search SoundCloud with scsearch8 using "{title} {artist}".
        Returns temp file path (MP3 320kbps) or None.
        Always returns None on failure — scsearch is a known flaky extractor
        and should never raise into the tier chain or penalise the YouTube CB.
        """
        if not self._rate_limiter.is_healthy("soundcloud"):
            logger.warning("SoundCloud circuit breaker open; skipping Tier 5.")
            return None

        if not self._throttle.wait("soundcloud"):
            logger.info("Throttle skip: soundcloud track %d — will retry next run", track.id)
            return None

        query = f"scsearch8:{track.title} {track.artist}"
        out_stem = os.path.join(TEMP_DIR, str(uuid.uuid4()))
        ydl_opts = self._build_mp3_opts(out_stem)
        ydl_opts["noplaylist"] = False
        ydl_opts["ignoreerrors"] = True
        ydl_opts["max_downloads"] = 1

        try:
            with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                ydl.download([query])
        except yt_dlp.utils.MaxDownloadsReached:
            pass  # expected: raised after max_downloads=1 succeeds
        except Exception as exc:
            if not self._is_content_error(exc):
                self._rate_limiter.record_failure("soundcloud")
            logger.warning("Tier 5 SoundCloud failed for track %d ('%s'): %s", track.id, track.title, exc)
            return None

        downloaded = self._find_output_file(out_stem)
        if downloaded and os.path.exists(downloaded) and os.path.getsize(downloaded) > 0:
            self._rate_limiter.record_success("soundcloud")
            self._throttle.on_success("soundcloud")
            return downloaded

        return None

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
        Returns True if the track has ≥25 failed attempts recorded.
        """
        failed_count = session.execute(
            select(func.count(DownloadAttempt.id)).where(
                DownloadAttempt.track_id == track_id,
                DownloadAttempt.success == False,  # noqa: E712
            )
        ).scalar_one()

        return failed_count >= _GIVE_UP_THRESHOLD

    # ── Internal helpers ───────────────────────────────────────────────────────

    @staticmethod
    def _get_spotdl_client(client_id: str, client_secret: str) -> Optional[Any]:
        """Return the process-wide Spotdl singleton, creating it on first call.

        SpotifyClient is a class-level singleton inside spotdl — constructing a
        second Spotdl instance in the same process raises "already initialized".
        A single instance is created once and reused for all downloads.
        """
        global _SPOTDL_CLIENT
        if not SPOTDL_AVAILABLE:
            return None
        if _SPOTDL_CLIENT is not None:
            return _SPOTDL_CLIENT
        with _SPOTDL_CLIENT_LOCK:
            if _SPOTDL_CLIENT is None:
                loop = asyncio.new_event_loop()
                _SPOTDL_CLIENT = Spotdl(
                    client_id=client_id,
                    client_secret=client_secret,
                    downloader_settings={
                        "output": os.path.abspath(TEMP_DIR),
                        "format": "mp3",
                        "bitrate": "320k",
                        "overwrite": "force",
                        "log_level": "ERROR",
                    },
                    loop=loop,
                )
                logger.info("spotdl singleton initialised (client_id=%s…)", client_id[:8])
        return _SPOTDL_CLIENT

    def _build_mp3_opts(self, out_stem: str) -> dict:
        """
        Build yt-dlp options for bestaudio → FFmpeg → MP3 320kbps output.
        Output template uses the stem; the final file will be {stem}.mp3.
        """
        opts: dict = {
            # Flexible format selector accepts any available audio stream.
            # Relies on FFmpeg post-processing to normalize output to MP3 320kbps.
            # This eliminates format availability as a failure point.
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
            # Add per-request sleep at the yt-dlp level (on top of our throttle).
            # This slows down info extraction HTTP calls and reduces 429 likelihood.
            "sleep_interval_requests": 1,
            # Android player client returns pre-signed format URLs that bypass
            # YouTube's JS nsig decryption challenge — no JS runtime required.
            # Without this, modern music content returns "format not available".
            "extractor_args": {
                "youtube": {"player_client": ["android", "web"]},
            },
        }

        cookies_src = "cookies.txt"
        if os.path.exists(cookies_src) and os.path.getsize(cookies_src) > 0:
            if os.access(cookies_src, os.W_OK):
                opts["cookiefile"] = cookies_src
            else:
                # Docker mounts cookies.txt :ro — yt-dlp tries to write-lock it
                # on open, causing EROFS. Copy to a writable temp file instead.
                import shutil
                import tempfile
                try:
                    tmp = tempfile.NamedTemporaryFile(
                        suffix=".txt", delete=False, dir=TEMP_DIR
                    )
                    shutil.copy2(cookies_src, tmp.name)
                    tmp.close()
                    opts["cookiefile"] = tmp.name
                except Exception as exc:
                    logger.debug("Could not copy cookies.txt to temp: %s", exc)

        return opts

    def _find_output_file(self, out_stem: str) -> Optional[str]:
        """Find the downloaded file matching the stem (any audio extension)."""
        for ext in ("mp3", "m4a", "opus", "webm", "flac", "ogg"):
            candidate = out_stem + f".{ext}"
            if os.path.exists(candidate):
                return candidate
        return None

    @staticmethod
    def _exc_contains_rate_limit(exc: BaseException) -> bool:
        """Walk the full exception chain looking for 429/rate-limit signals.

        SpotiFLAC wraps backend 429s in generic messages like "Deezer download
        failed", so str(exc) alone misses them.  Checking __cause__/__context__
        catches the original HTTPError before it gets re-wrapped.
        """
        seen: set[int] = set()
        current: Optional[BaseException] = exc
        while current is not None and id(current) not in seen:
            seen.add(id(current))
            text = (str(current) + repr(current)).lower()
            if any(x in text for x in ("429", "too many requests", "rate limit", "rate-limit", "ratelimit")):
                return True
            current = current.__cause__ or current.__context__
        return False

    @staticmethod
    def _is_youtube_session_rate_limited(exc: Exception) -> bool:
        """Return True when YouTube has rate-limited the entire session.

        Per-video content errors use _is_content_error; this specifically
        catches the session-level block that makes every subsequent request fail.
        """
        msg = str(exc).lower()
        return "session has been rate-limited" in msg or "rate-limited by youtube" in msg

    @staticmethod
    def _is_content_error(exc: Exception) -> bool:
        """
        Returns True when the error is video/content-specific rather than a
        service-level failure.  These must NOT trip the circuit breaker because
        the service itself is healthy — only a specific piece of content failed.
        """
        msg = str(exc).lower()
        return any(phrase in msg for phrase in (
            "requested format is not available",
            "private video",
            "video unavailable",
            "this video is not available",
            "has been removed",
            "sign in to confirm",
            "requires payment",
            "copyright",
            "geographic restriction",
            "not available in your country",
        ))

    @staticmethod
    def _resolve_method_label(tier_name: str, file_path: str) -> str:
        """
        Map internal tier name + file path to the canonical download_method label.

        Labels per spec:
          spotiflac | ytdlp_ytm | spotdl | ytdlp_yt | ytdlp_soundcloud
        """
        if tier_name == "tier1_spotiflac":
            # SpotiFLAC names output files by track title, not by service —
            # there is no reliable way to recover which backend succeeded.
            return "spotiflac"
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
