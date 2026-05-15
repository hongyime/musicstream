"""
musicstream/ingestion/downloader.py — 5-tier download orchestrator

Implements the full tier chain for downloading tracks:
  Tier 2: yt-dlp + ytmusicapi (songs→videos→no filter) — MP3 320kbps, ±5s duration check
  Tier 3: spotdl Python API — MP3 320kbps, requires Spotify credentials
  Tier 4: yt-dlp YouTube direct search (ytsearch12) — MP3 320kbps
  Tier 5: yt-dlp SoundCloud (scsearch8) — MP3 320kbps, uses separate "soundcloud" circuit breaker

After ≥25 failed attempts: status='failed', log [DOWNLOAD_FAIL] to errors.log.
MAX_CONCURRENT = 4 parallel workers via ThreadPoolExecutor.
"""

from __future__ import annotations

import logging
import os
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import yt_dlp  # type: ignore[import-untyped]
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from src.exceptions import DownloadError, OrganiserError, TaggingError
from src.models import DownloadAttempt, Track, TrackStatus
from src.rate_limiter import ServiceRateLimiter, ServiceThrottle

logger = logging.getLogger(__name__)
errors_logger = logging.getLogger("errors")

# ── librespot optional import (Tier 0: direct Spotify CDN) ───────────────────

try:
    from librespot.core import Session as _LibrespotSession
    from librespot.metadata import TrackId as _TrackId
    from librespot.audio.decoders import (
        VorbisOnlyAudioQuality as _VorbisQuality,
        AudioQuality as _AudioQuality,  # AudioQuality lives in decoders, not audio
    )
    LIBRESPOT_AVAILABLE = True
except ImportError:
    LIBRESPOT_AVAILABLE = False
    logger.warning("librespot not available; Tier 0 skipped")

if LIBRESPOT_AVAILABLE:
    logger.info("librespot available — Tier 0 active")

# Module-level librespot session singleton (created once, reused across all workers)
_librespot_session: Optional[object] = None
_librespot_session_lock = threading.Lock()


def _get_librespot_session():
    """Return the shared librespot Session, creating it on first call."""
    global _librespot_session
    if _librespot_session is not None:
        return _librespot_session
    with _librespot_session_lock:
        if _librespot_session is not None:
            return _librespot_session
        cred_file = os.environ.get("LIBRESPOT_CREDENTIALS_FILE", "/app/data/librespot_credentials.json")
        username = os.environ.get("SPOTIFY_USERNAME", "").strip()
        password = os.environ.get("SPOTIFY_PASSWORD", "").strip()
        # Ensure credential file directory exists inside container
        import pathlib as _pl
        _pl.Path(cred_file).parent.mkdir(parents=True, exist_ok=True)
        conf = _LibrespotSession.Configuration.Builder() \
            .set_stored_credential_file(cred_file) \
            .build()
        # Try stored credentials first (no password needed after first auth)
        cred_exists = _pl.Path(cred_file).exists() and _pl.Path(cred_file).stat().st_size > 10
        if cred_exists:
            try:
                _librespot_session = _LibrespotSession.Builder(conf).stored_file().create()
                logger.info("librespot: authenticated from stored credential file")
                return _librespot_session
            except Exception as exc:
                logger.warning("librespot: stored credential failed (%s), trying password", exc)
        if not username or not password:
            raise RuntimeError("SPOTIFY_USERNAME/PASSWORD not set and no valid credential file at " + cred_file)
        _librespot_session = _LibrespotSession.Builder(conf).user_pass(username, password).create()
        logger.info("librespot: authenticated with username/password → credentials saved to %s", cred_file)
        return _librespot_session


# ── ytmusicapi optional import ─────────────────────────────────────────────────

try:
    from ytmusicapi import YTMusic  # type: ignore[import-untyped]
    YTMUSICAPI_AVAILABLE = True
except ImportError:
    YTMUSICAPI_AVAILABLE = False
    logger.warning("ytmusicapi not available; Tier 2 will be skipped")

# ── spotdl optional import ─────────────────────────────────────────────────────

try:
    import spotdl  # type: ignore[import-untyped]  # noqa: F401
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

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class DownloadOrchestrator:
    """
    5-tier download orchestrator.

    Downloads all pending tracks in parallel batches of MAX_CONCURRENT.
    Each track runs through the full tier chain; a single track failure
    never raises an exception that stops other downloads.
    """

    MAX_CONCURRENT: int = MAX_CONCURRENT

    def __init__(self) -> None:
        # Trip after one full round of all workers failing plus a small buffer.
        # MAX_CONCURRENT*5 (=60) was too lenient — a broken service would rack up
        # 60 consecutive failures before the breaker opened.  MAX_CONCURRENT+5
        # trips within two bad rounds, which is fast enough to matter.
        circuit_threshold = MAX_CONCURRENT + 5
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
        # Tracks left in DOWNLOADING from a previous crashed/restarted run are
        # permanently stuck — no worker will pick them up again.  Reset them here
        # before querying PENDING so they re-enter the queue this run.
        stuck = (
            session.execute(
                select(Track).where(Track.status == TrackStatus.DOWNLOADING.value)
            )
            .scalars()
            .all()
        )
        if stuck:
            for t in stuck:
                t.status = TrackStatus.PENDING.value
            session.flush()
            logger.info("Reset %d stuck DOWNLOADING tracks to PENDING", len(stuck))

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

    def download_pending_librespot(
        self,
        session: Session,
        per_track_timeout: float = 90.0,
        max_seconds: float = 7200.0,
    ) -> tuple[int, int]:
        """
        Single-worker librespot pre-sweep: every pending track gets a genuine
        Spotify CDN attempt before the 12-worker batch tries YouTube sources.

        Two-level time control:
          per_track_timeout — max seconds for a single track's librespot attempt.
            stream.read() is blocking, so we run each attempt in a thread and
            join with this timeout.  Prevents one hung connection blocking the sweep.
          max_seconds — total budget for the whole sweep.  Stops early so the
            12-worker batch + spotdl sweep still run in the same cycle.

        Returns:
            (downloaded, failed) counts.
        """
        if not LIBRESPOT_AVAILABLE:
            logger.info("librespot not available; skipping pre-sweep.")
            return 0, 0

        from concurrent.futures import ThreadPoolExecutor, TimeoutError as FuturesTimeout
        from src.db import get_session

        pending = (
            session.execute(
                select(Track).where(Track.status == TrackStatus.PENDING.value)
            )
            .scalars()
            .all()
        )

        if not pending:
            return 0, 0

        logger.info(
            "librespot pre-sweep: %d pending tracks (per_track=%.0fs total=%.0fs)",
            len(pending), per_track_timeout, max_seconds,
        )
        downloaded = 0
        failed = 0
        sweep_start = time.monotonic()
        _lib_tiers = [("tier0_librespot", self._tier0_librespot)]

        with ThreadPoolExecutor(max_workers=1) as executor:
            for track in pending:
                if time.monotonic() - sweep_start > max_seconds:
                    logger.info("librespot pre-sweep: total time limit reached after %.0fs", max_seconds)
                    break
                if not track.spotify_id:
                    continue
                try:
                    with get_session() as ts:
                        t = ts.get(Track, track.id)
                        if t is None or t.status != TrackStatus.PENDING.value:
                            continue
                        future = executor.submit(
                            self.download_track, t, ts, _lib_tiers
                        )
                        try:
                            success = future.result(timeout=per_track_timeout)
                        except FuturesTimeout:
                            future.cancel()
                            logger.warning(
                                "librespot per-track timeout (%.0fs) for track %d ('%s')",
                                per_track_timeout, track.id, track.title,
                            )
                            success = False
                        if success:
                            downloaded += 1
                        else:
                            failed += 1
                except Exception as exc:
                    logger.error("librespot sweep error for track %d: %s", track.id, exc, exc_info=True)
                    failed += 1

        elapsed = time.monotonic() - sweep_start
        logger.info(
            "librespot pre-sweep done: downloaded=%d failed=%d elapsed=%.0fs",
            downloaded, failed, elapsed,
        )
        return downloaded, failed

    def download_pending_spotdl(self, session: Session, max_seconds: float = 3600.0) -> tuple[int, int]:
        """
        Single-worker spotdl sweep run after the main 12-worker batch.

        Processes ALL still-PENDING tracks (not a fixed top-N — spotdl's
        Spotify-metadata search has genuinely different coverage from tier2's
        raw query, so every remaining track deserves an attempt).

        Stops after max_seconds so the daemon cycle doesn't overrun.

        Returns:
            (downloaded, failed) counts.
        """
        from src.db import get_session

        candidates = (
            session.execute(
                select(Track).where(Track.status == TrackStatus.PENDING.value)
            )
            .scalars()
            .all()
        )

        if not candidates:
            logger.info("spotdl sweep: no pending tracks to process.")
            return 0, 0

        logger.info("spotdl sweep: %d tracks (max %.0fs)", len(candidates), max_seconds)
        downloaded = 0
        failed = 0
        sweep_start = time.monotonic()
        _spotdl_tiers = [("tier3_spotdl", self._tier3_spotdl)]

        for track in candidates:
            if time.monotonic() - sweep_start > max_seconds:
                logger.info("spotdl sweep: time limit reached after %.0fs", max_seconds)
                break
            try:
                with get_session() as ts:
                    t = ts.get(Track, track.id)
                    if t is None or t.status != TrackStatus.PENDING.value:
                        continue
                    success = self.download_track(t, ts, tiers_override=_spotdl_tiers)
                    if success:
                        downloaded += 1
                    else:
                        failed += 1
            except Exception as exc:
                logger.error("spotdl sweep unhandled error for track %d: %s", track.id, exc, exc_info=True)
                failed += 1

        elapsed = time.monotonic() - sweep_start
        logger.info("spotdl sweep done: downloaded=%d failed=%d elapsed=%.0fs", downloaded, failed, elapsed)
        return downloaded, failed

    def download_track(self, track: Track, session: Session, tiers_override=None) -> bool:
        """
        Run the tier chain for a single track.

        Records every attempt in download_attempts. On success, updates
        track.status and track.download_method. On exhaustion, marks
        status='failed' if ≥9 failed attempts.

        Returns:
            True if the track was successfully downloaded, False otherwise.
        """
        # Mark as downloading
        track.status = TrackStatus.DOWNLOADING.value
        session.flush()

        # Tier 0 (librespot) and tier 3 (spotdl) are single-worker sweeps that
        # run before/after this batch — not in the 12-worker pool.  Both require
        # serialised access; running them here would block 11/12 workers on a
        # semaphore or produce 11x the Spotify API hammering.
        tiers = tiers_override if tiers_override is not None else [
            ("tier2_ytdlp_ytm",        self._tier2_ytdlp_ytm),
            ("tier4_ytdlp_youtube",    self._tier4_ytdlp_youtube),
            ("tier5_ytdlp_soundcloud", self._tier5_ytdlp_soundcloud),
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

    # ── Tier 0: librespot (direct Spotify CDN) ───────────────────────────────

    def _tier0_librespot(self, track: Track) -> Optional[str]:
        """
        Download directly from Spotify's CDN via librespot.
        Streams OGG Vorbis (320kbps equivalent) and converts to MP3 320k via FFmpeg.
        Requires SPOTIFY_USERNAME + SPOTIFY_PASSWORD on first run only; subsequent
        runs use the stored credential blob at LIBRESPOT_CREDENTIALS_FILE.
        Returns temp MP3 path or None.
        """
        if not LIBRESPOT_AVAILABLE:
            return None

        if not self._rate_limiter.is_healthy("librespot"):
            logger.warning("librespot circuit breaker open; skipping Tier 0.")
            return None

        if not track.spotify_id:
            logger.debug("Tier 0 skipped for track %d: no spotify_id", track.id)
            return None

        try:
            _librespot_auth_ok = False
            session = _get_librespot_session()
            _librespot_auth_ok = True

            track_id = _TrackId.from_uri(track.spotify_uri)
            stream = session.content_feeder().load(
                track_id,
                _VorbisQuality(_AudioQuality.VERY_HIGH),
                False,
                None,
            )

            # Write OGG Vorbis to temp file
            out_dir = os.path.join(TEMP_DIR, f"librespot_{track.id}_{uuid.uuid4().hex[:8]}")
            os.makedirs(out_dir, exist_ok=True)
            ogg_path = os.path.join(out_dir, f"{uuid.uuid4().hex}.ogg")

            with open(ogg_path, "wb") as fh:
                while True:
                    chunk = stream.input_stream.stream().read(65536)
                    if not chunk:
                        break
                    fh.write(chunk)

            if not os.path.exists(ogg_path) or os.path.getsize(ogg_path) < 1024:
                logger.warning("librespot: empty stream for track %d ('%s')", track.id, track.title)
                self._rate_limiter.record_failure("librespot")
                return None

            # Convert OGG → MP3 320k
            mp3_path = ogg_path.replace(".ogg", ".mp3")
            import subprocess as _sp
            ffmpeg_result = _sp.run(
                [
                    "ffmpeg", "-i", ogg_path,
                    "-codec:a", "libmp3lame", "-b:a", "320k",
                    "-y", "-loglevel", "error",
                    mp3_path,
                ],
                capture_output=True,
                timeout=120,
            )
            try:
                os.remove(ogg_path)
            except OSError:
                pass

            mp3_size = os.path.getsize(mp3_path) if os.path.exists(mp3_path) else 0
            if ffmpeg_result.returncode != 0 or mp3_size < 10_240:
                logger.warning(
                    "librespot: ffmpeg conversion failed for track %d (size=%d): %s",
                    track.id, mp3_size, ffmpeg_result.stderr.decode(errors="replace")[:200],
                )
                self._rate_limiter.record_failure("librespot")
                return None

            self._rate_limiter.record_success("librespot")
            logger.info("librespot: track %d ('%s') downloaded OK", track.id, track.title)
            return mp3_path

        except Exception as exc:
            self._rate_limiter.record_failure("librespot")
            logger.warning("librespot failed for track %d ('%s'): %s", track.id, track.title, exc)
            # Only invalidate the session on auth failures or explicit auth signals.
            # Transient stream errors (IOError, empty chunk, decode error) don't
            # mean the session is dead — nuking it on every blip forces expensive
            # full re-auth on the next call.
            exc_str = str(exc).lower()
            is_auth_failure = not _librespot_auth_ok or any(
                kw in exc_str for kw in ("auth", "credential", "login", "token", "unauthorized", "403", "invalid")
            )
            if is_auth_failure:
                global _librespot_session
                _librespot_session = None
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
        Download via spotdl CLI using the Spotify URI. Output: MP3 320kbps to temp/.
        Returns temp file path or None.
        Requires SPOTIFY_CLIENT_ID and SPOTIFY_CLIENT_SECRET env vars.
        
        Using CLI instead of Python API to avoid asyncio/threading conflicts.
        The spotdl Python API has a process-wide SpotifyClient singleton that
        conflicts with ThreadPoolExecutor workers.
        """
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

        try:
            import subprocess
            import shutil
            
            # Check if spotdl CLI is available
            spotdl_path = shutil.which("spotdl")
            if not spotdl_path:
                logger.warning("spotdl CLI not found; skipping Tier 3.")
                self._rate_limiter.record_failure("spotdl")
                return None

            spotify_uri = track.spotify_uri
            if not spotify_uri:
                logger.warning("Tier 3 skipped for track %d ('%s'): no spotify_uri", track.id, track.title)
                return None
            
            # Use a unique output directory for this download
            out_dir = os.path.join(TEMP_DIR, f"spotdl_{track.id}_{uuid.uuid4().hex[:8]}")
            os.makedirs(out_dir, exist_ok=True)

            # URI must come BEFORE --audio: spotdl's --audio uses nargs=* and will
            # greedily consume the URI as an audio-source value, failing argparse validation.
            base_cmd = [
                spotdl_path,
                spotify_uri,
                "--output", out_dir,
                "--format", "mp3",
                "--bitrate", "320k",
                "--log-level", "ERROR",
                "--client-id", client_id,
                "--client-secret", client_secret,
            ]

            # Attempt 1: youtube-music (Spotify-matched, most accurate)
            cmd_ytm = base_cmd + ["--audio", "youtube-music"]
            logger.debug("Running spotdl (youtube-music) for track %d", track.id)
            result = subprocess.run(
                cmd_ytm,
                capture_output=True,
                text=True,
                timeout=120,
            )

            # Attempt 2: fallback to plain youtube search
            if result.returncode != 0:
                logger.info(
                    "spotdl --audio youtube-music failed for track %d ('%s'); falling back to youtube: %s",
                    track.id, track.title, result.stderr[:120],
                )
                cmd_yt = base_cmd + ["--audio", "youtube"]
                result = subprocess.run(
                    cmd_yt,
                    capture_output=True,
                    text=True,
                    timeout=120,
                )

            if result.returncode != 0:
                logger.warning(
                    "spotdl CLI failed for track %d ('%s'): returncode=%d, stderr=%s",
                    track.id, track.title, result.returncode, result.stderr[:200],
                )
                self._rate_limiter.record_failure("spotdl")
                return None

            # Find downloaded file
            downloaded_file = None
            for root, _, files in os.walk(out_dir):
                for fname in files:
                    if fname.endswith((".mp3", ".m4a", ".flac", ".ogg", ".opus")):
                        full_path = os.path.join(root, fname)
                        if os.path.getsize(full_path) > 0:
                            downloaded_file = full_path
                            break
                if downloaded_file:
                    break

            if not downloaded_file:
                logger.warning("spotdl CLI did not produce a file for track %d", track.id)
                self._rate_limiter.record_failure("spotdl")
                return None

            self._rate_limiter.record_success("spotdl")
            self._throttle.on_success("spotdl")
            logger.info("spotdl CLI successfully downloaded track %d via %s", track.id, os.path.basename(downloaded_file))
            return downloaded_file

        except subprocess.TimeoutExpired:
            logger.warning("spotdl CLI timeout for track %d", track.id)
            self._rate_limiter.record_failure("spotdl")
            return None
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

        Walk the full exception chain — some backends wrap 429s in generic messages,
        so checking __cause__/__context__ catches re-wrapped HTTPErrors.
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
          librespot | ytdlp_ytm | spotdl | ytdlp_yt | ytdlp_soundcloud
        """
        if tier_name == "tier0_librespot":
            return "librespot"
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
