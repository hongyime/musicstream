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
import shutil
import tempfile
import threading
import time
import uuid
import inspect
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from typing import Optional

import yt_dlp  # type: ignore[import-untyped]
from sqlalchemy import select
from sqlalchemy.orm import Session

from src.exceptions import DownloadError, OrganiserError, TaggingError
from src.models import DownloadAttempt, Track, TrackStatus
from src.rate_limiter import ServiceRateLimiter, ServiceThrottle
from src.ingestion import tier_errors as te

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


# ── SpotiFLAC optional import (Tier 1: Lossless from other services) ──────────

try:
    from SpotiFLAC import SpotiFLAC as _SpotiFLAC
    SPOTIFLAC_AVAILABLE = True
except ImportError:
    SPOTIFLAC_AVAILABLE = False
    logger.warning("SpotiFLAC not available; Tier 1 will be skipped")

if SPOTIFLAC_AVAILABLE:
    logger.info("SpotiFLAC available — Tier 1 active")
    _SPOTIFLAC_PARAMS = set(inspect.signature(_SpotiFLAC).parameters)
else:
    _SPOTIFLAC_PARAMS = set()

# SpotiFLAC serialisation — streaming services return 429 under concurrent load.
_SPOTIFLAC_SEMAPHORE = threading.Semaphore(1)

# Librespot serialisation — single Spotify streaming session per account, parallel
# stream loads cause 90s timeouts and circuit-breaker trips. Force strictly serial
# librespot use across all workers; other tiers (SpotiFLAC, yt-dlp, spotdl) stay
# parallel so the worker pool isn't bottlenecked when librespot is skipped.
_LIBRESPOT_SEMAPHORE = threading.Semaphore(1)

# Single-flight kill signal for the librespot pre-sweep timeout watchdog: set by
# the sweep's main thread right before it nulls the streaming session to unblock a
# hung read, and checked by download_track's loop so the killed worker does NOT also
# record an attempt (the main thread writes the authoritative rate_limited row).
# Safe as one global because the sweep is max_workers=1 — exactly one in-flight attempt.
_librespot_kill_event = threading.Event()
_LIBRESPOT_KILL_GRACE_S = 30.0  # max wait for a killed worker to observe + exit


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
_GIVE_UP_THRESHOLD = 20    # ~4 complete tier-chain runs before giving up

# Worker concurrency — configurable via environment variable
# Default: 4 workers, can be increased to 6 or 8 for faster downloads
# Note: Increasing significantly may trigger API rate limits
MAX_CONCURRENT = int(os.environ.get("MAX_CONCURRENT_WORKERS", "4"))
logger.info("Worker concurrency set to: MAX_CONCURRENT=%d", MAX_CONCURRENT)

# ── P0-2: cooperative drain-on-shutdown ──────────────────────────────────────
# Lifespan shutdown (daemon.py) calls request_shutdown() on SIGTERM. The three
# sweep loops below (download_pending batches, librespot pre-sweep, spotdl sweep)
# check _shutting_down between tracks and stop claiming new work, so an in-flight
# row is the only one that can be left DOWNLOADING — and lifespan resets that.
# threading.Event is used because the sweeps run in worker threads.
_shutting_down = threading.Event()


def request_shutdown() -> None:
    """Signal all download sweeps to stop picking up new tracks (P0-2)."""
    _shutting_down.set()

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

    # Thread-local channel: a tier records WHY it returned None so the download
    # loop can persist the real reason into download_attempts.error instead of a
    # generic placeholder (Oracle review). Class-level so it survives
    # DownloadOrchestrator.__new__(...) used in tests and is per-thread-isolated
    # across the worker pool.
    _fail_tls = threading.local()

    def __init__(self) -> None:
        # Check if Tier 1 (SpotiFLAC) is enabled
        self._tier1_enabled = os.environ.get("ENABLE_TIER1", "true").lower() == "true"
        logger.info("Tier 1 (SpotiFLAC) %s", "enabled" if self._tier1_enabled else "disabled via ENABLE_TIER1=false")

        # SpotiFLAC sub-provider list (controllable via env).
        # Default skips tidal+amazon — Tidal mirrors are returning 403/502/timeouts
        # cluster-wide and Amazon Songlink resolution is broken upstream.  Order
        # matters: first hit wins, so put healthy providers first.
        # Override with: SPOTIFLAC_SERVICES=qobuz,deezer,youtube,tidal,amazon
        _raw_services = os.environ.get("SPOTIFLAC_SERVICES", "qobuz,deezer,youtube")
        self._spotiflac_services = [
            s.strip().lower() for s in _raw_services.split(",") if s.strip()
        ] or ["qobuz", "deezer", "youtube"]
        logger.info("SpotiFLAC providers (in order): %s", ",".join(self._spotiflac_services))

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

        # Track ephemeral cookies copies so they can be cleaned at exit.
        # Each yt-dlp call creates one if cookies.txt is read-only (Docker
        # bind-mount); without explicit tracking they accumulate forever.
        # (audit #20)
        self._tmp_cookie_files: set[str] = set()

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
        #
        # CRITICAL race fix (audit #11): the previous version reset EVERY row
        # in DOWNLOADING state. If two scheduler runs (or a manual /sync
        # trigger landing on top of a cron-run) overlapped, the second run
        # would yank tracks out from under workers in the FIRST run, causing
        # double downloads, orphaned temp files, and DB UPDATE conflicts.
        #
        # Fix: only reset rows whose updated_at is older than the worker
        # heartbeat window (default 30 min). A live worker bumps updated_at
        # on each tier transition + on success/failure — anything older than
        # 30 min is provably crashed.
        from datetime import datetime, timedelta, timezone
        stuck_cutoff = datetime.now(timezone.utc) - timedelta(minutes=30)
        stuck = (
            session.execute(
                select(Track).where(
                    Track.status == TrackStatus.DOWNLOADING.value,
                    Track.updated_at < stuck_cutoff,
                )
            )
            .scalars()
            .all()
        )
        if stuck:
            for t in stuck:
                t.status = TrackStatus.PENDING.value
            session.flush()
            logger.info(
                "Reset %d stuck DOWNLOADING tracks (updated_at < %s) to PENDING",
                len(stuck), stuck_cutoff.isoformat(),
            )

        # P2-4 memory fix: hydrate only Track.id — the batch loop below never
        # touches other columns (per-track workers re-fetch via session.get in
        # their own session).  Loading full Track rows for 100k+ pending tracks
        # was pushing the Postgres 128M container over its cap and OOM-killing
        # the server (see incident 2026-07-08).
        track_ids = list(
            session.execute(
                select(Track.id).where(Track.status == TrackStatus.PENDING.value)
            ).scalars()
        )

        if not track_ids:
            logger.info("No pending tracks to download.")
            return 0, 0

        logger.info("Starting download of %d pending tracks.", len(track_ids))

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

        # Process in batches with delays between batches to avoid rate limits
        batch_size = MAX_CONCURRENT
        total_batches = (len(track_ids) + batch_size - 1) // batch_size

        for batch_num in range(total_batches):
            if _shutting_down.is_set():
                logger.warning(
                    "Shutdown requested; stopping download_pending after %d/%d batches.",
                    batch_num, total_batches,
                )
                break
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

        # P2-4 memory fix: fetch only the three columns the loop actually reads
        # (id, spotify_id, title) so 100k+ pending tracks don't materialise as
        # full ORM rows in Postgres RAM. See download_pending() for context.
        pending = list(
            session.execute(
                select(Track.id, Track.spotify_id, Track.title).where(
                    Track.status == TrackStatus.PENDING.value
                )
            )
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

        if not self._rate_limiter.is_healthy("librespot"):
            logger.warning("librespot circuit breaker open; skipping Tier 0 pre-sweep entirely.")
            return 0, 0

        _lib_tiers = [("tier0_librespot", self._tier0_librespot)]

        def _librespot_one(track_id: int) -> bool:
            """Download one track via librespot in its OWN session — no cross-thread
            session sharing (mirrors download_pending's worker pattern)."""
            with get_session() as ws:
                t = ws.get(Track, track_id)
                if t is None or t.status != TrackStatus.PENDING.value:
                    return False
                return self.download_track(t, ws, _lib_tiers)

        with ThreadPoolExecutor(max_workers=1) as executor:
            for track_id, track_spotify_id, track_title in pending:
                if _shutting_down.is_set():
                    logger.warning("Shutdown requested; aborting librespot pre-sweep.")
                    break
                if not self._rate_limiter.is_healthy("librespot"):
                    logger.warning("librespot circuit breaker tripped; aborting pre-sweep.")
                    break
                if time.monotonic() - sweep_start > max_seconds:
                    logger.info("librespot pre-sweep: total time limit reached after %.0fs", max_seconds)
                    break
                if not track_spotify_id:
                    continue
                try:
                    future = executor.submit(_librespot_one, track_id)
                    try:
                        success = future.result(timeout=per_track_timeout)
                    except FuturesTimeout:
                        future.cancel()
                        logger.warning(
                            "librespot per-track timeout (%.0fs) for track %d ('%s')",
                            per_track_timeout, track_id, track_title,
                        )
                        # F1: timeouts are ALSO failures so the rate-limiter trips and
                        # we stop hammering an already-rate-limited account (#325).
                        self._rate_limiter.record_failure("librespot")
                        # Tell the worker THIS attempt was killed so its download_track
                        # loop skips recording — the main thread writes the single
                        # authoritative rate_limited row below. Single global is safe:
                        # max_workers=1 means exactly one in-flight librespot attempt.
                        _librespot_kill_event.set()
                        # F3: future.cancel() is a no-op on a worker blocked in a C-level
                        # socket read. Nulling the singleton makes the next read raise
                        # (closed socket) so the hung worker exits; the session rebuilds.
                        global _librespot_session
                        try:
                            old = _librespot_session
                            _librespot_session = None
                            if old is not None and hasattr(old, "close"):
                                old.close()
                        except Exception as _close_exc:  # noqa: BLE001
                            logger.debug("librespot session close on timeout: %s", _close_exc)
                        # Wait briefly for the killed worker to observe the kill and exit
                        # before we record + clear, so it cannot race us into a double row.
                        try:
                            future.result(timeout=_LIBRESPOT_KILL_GRACE_S)
                        except Exception:  # noqa: BLE001 - grace timeout or worker error
                            pass
                        self._record_librespot_timeout(track_id)
                        _librespot_kill_event.clear()
                        success = False
                    if success:
                        downloaded += 1
                    else:
                        failed += 1
                except Exception as exc:
                    logger.error("librespot sweep error for track %d: %s", track_id, exc, exc_info=True)
                    failed += 1
                # F2: pace inter-track attempts. Upstream contributor (cvdub)
                # confirms <5s between streams reliably triggers Spotify's
                # per-account rate limit, after which the account is dead for
                # 1-2 hours.  Override with LIBRESPOT_INTER_TRACK_SLEEP=0 for
                # tests; default 10s in production.
                _inter_sleep = float(os.environ.get("LIBRESPOT_INTER_TRACK_SLEEP", "10"))
                if _inter_sleep > 0:
                    time.sleep(_inter_sleep)

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

        # P2-4 memory fix: hydrate only Track.id — the loop below re-fetches via
        # session.get() in its own session anyway. Loading full rows for 100k+
        # pending tracks OOM-killed the 128M Postgres container (2026-07-08).
        candidate_ids = list(
            session.execute(
                select(Track.id).where(Track.status == TrackStatus.PENDING.value)
            ).scalars()
        )

        if not candidate_ids:
            logger.info("spotdl sweep: no pending tracks to process.")
            return 0, 0

        logger.info("spotdl sweep: %d tracks (max %.0fs)", len(candidate_ids), max_seconds)
        downloaded = 0
        failed = 0
        sweep_start = time.monotonic()
        _spotdl_tiers = [("tier3_spotdl", self._tier3_spotdl)]

        for track_id in candidate_ids:
            if _shutting_down.is_set():
                logger.warning("Shutdown requested; aborting spotdl sweep.")
                break
            if time.monotonic() - sweep_start > max_seconds:
                logger.info("spotdl sweep: time limit reached after %.0fs", max_seconds)
                break
            try:
                with get_session() as ts:
                    t = ts.get(Track, track_id)
                    if t is None or t.status != TrackStatus.PENDING.value:
                        continue
                    success = self.download_track(t, ts, tiers_override=_spotdl_tiers)
                    if success:
                        downloaded += 1
                    else:
                        failed += 1
            except Exception as exc:
                logger.error("spotdl sweep unhandled error for track %d: %s", track_id, exc, exc_info=True)
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
        # Audit #33: bind track_id to the logging context for the entire
        # tier chain. Every log line emitted by tagger / organiser / rate
        # limiter / ffmpeg subprocess wrapper inherits the contextvar so
        # `grep "\[42\]" logs/musicstream.log` returns this track's full
        # ingestion timeline.
        from src.logging_context import track_context
        with track_context(track.id):
            return self._download_track_inner(track, session, tiers_override)

    def _download_track_inner(self, track: Track, session: Session, tiers_override=None) -> bool:
        # Atomic claim (audit #12): convert from "set status; flush" (which
        # any two workers could both win) to a conditional UPDATE that only
        # succeeds if the row is STILL pending. If rowcount==0 another
        # worker beat us — return False so the caller skips it.
        from sqlalchemy import update
        result = session.execute(
            update(Track)
            .where(
                Track.id == track.id,
                Track.status == TrackStatus.PENDING.value,
            )
            .values(status=TrackStatus.DOWNLOADING.value)
        )
        if result.rowcount == 0:
            logger.debug(
                "Track %d already claimed by another worker (status changed) — skipping",
                track.id,
            )
            session.rollback()
            return False
        session.commit()
        # Refresh the in-memory ORM object so subsequent reads see DOWNLOADING.
        session.refresh(track)

        # Tier 0 (librespot) is a single-worker sweep that runs before this
        # batch — not in the 12-worker pool. It requires serialised access;
        # running it here would block 11/12 workers on a semaphore or produce
        # 11x the Spotify API hammering.
        tiers = tiers_override if tiers_override is not None else [
            ("tier1_spotiflac",        self._tier1_spotiflac),
            ("tier2_ytdlp_ytm",        self._tier2_ytdlp_ytm),
            ("tier3_spotdl",           self._tier3_spotdl),
            ("tier4_ytdlp_youtube",    self._tier4_ytdlp_youtube),
            ("tier5_ytdlp_soundcloud", self._tier5_ytdlp_soundcloud),
        ]

        # Filter out disabled tiers
        if not self._tier1_enabled:
            tiers = [t for t in tiers if t[0] != "tier1_spotiflac"]

        for method_name, tier_fn in tiers:
            try:
                self._fail_tls.fail_reason = None
                path = tier_fn(track)
                if path:
                    self._record_attempt(
                        session, track.id, method_name, error=None, success=True
                    )
                    download_method = self._resolve_method_label(method_name, path)
                    track.download_method = download_method
                    track.last_attempt_at = _utcnow()
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
                            exc_info=True,
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
                    if _librespot_kill_event.is_set():
                        # Killed by the librespot timeout watchdog — the sweep's main
                        # thread records the authoritative rate_limited row; skip ours
                        # to avoid a double-record (Oracle review, Option B).
                        break
                    logger.info(
                        "Tier %s → no result for track %d ('%s'); trying next tier",
                        method_name, track.id, track.title,
                    )
                    self._record_attempt(
                        session,
                        track.id,
                        method_name,
                        error=getattr(self._fail_tls, "fail_reason", None) or te.UNKNOWN_TIER_FAIL,
                        success=False,
                    )
                    track.attempt_count = (track.attempt_count or 0) + 1
                    track.last_attempt_at = _utcnow()
            except Exception as exc:
                self._record_attempt(
                    session, track.id, method_name, error=str(exc), success=False
                )
                track.attempt_count = (track.attempt_count or 0) + 1
                track.last_attempt_at = _utcnow()
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
        Attempt FLAC download via SpotiFLAC.
        Tries services in order: qobuz, tidal, amazon, deezer, youtube.
        Returns the path to the downloaded FLAC file, or None on failure.
        """
        if not SPOTIFLAC_AVAILABLE:
            return None

        if not self._rate_limiter.is_healthy("spotiflac"):
            logger.warning("SpotiFLAC circuit breaker open; skipping Tier 1.")
            return None

        if not track.spotify_id:
            logger.debug("Tier 1 skipped for track %d: no spotify_id (LB-only track)", track.id)
            return None

        # SpotiFLAC needs a Spotify track URL, not a URI
        spotify_url = f"https://open.spotify.com/track/{track.spotify_id}"
        out_dir = os.path.join(TEMP_DIR, f"spotiflac_{uuid.uuid4().hex}")
        os.makedirs(out_dir, exist_ok=True)

        services = list(self._spotiflac_services)

        try:
            with _SPOTIFLAC_SEMAPHORE:
                # Iterate services manually to detect which one succeeds
                for service in services:
                    try:
                        kwargs = {
                            "url": spotify_url,
                            "output_dir": out_dir,
                            "services": [service],
                        }
                        if "quality" in _SPOTIFLAC_PARAMS:
                            kwargs["quality"] = "LOSSLESS"
                        if "log_level" in _SPOTIFLAC_PARAMS:
                            kwargs["log_level"] = logging.WARNING

                        _SpotiFLAC(**kwargs)

                        # Find the downloaded file
                        for root, _, files in os.walk(out_dir):
                            for fname in files:
                                if fname.endswith((".flac", ".m4a", ".mp3")):
                                    found = os.path.join(root, fname)
                                    if os.path.getsize(found) > 0:
                                        # Labeled path: {uuid}_{service}.ext
                                        ext = os.path.splitext(fname)[1]
                                        labeled = os.path.join(TEMP_DIR, f"{uuid.uuid4().hex}_{service}{ext}")
                                        os.rename(found, labeled)
                                        self._rate_limiter.record_success("spotiflac")
                                        return labeled
                    except Exception as exc:
                        logger.debug("SpotiFLAC service=%s failed for track %d: %s", service, track.id, exc)
                        continue

            self._rate_limiter.record_failure("spotiflac")
            return None

        except Exception as exc:
            self._rate_limiter.record_failure("spotiflac")
            logger.debug("SpotiFLAC failed for track %d: %s", track.id, exc)
            return None
        finally:
            # Always remove the per-call temp dir; success path already moved
            # the labeled file out to TEMP_DIR root, so this only cleans the
            # working directory and any leftover partials.
            shutil.rmtree(out_dir, ignore_errors=True)

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
            # Service name encoded in filename: {hex}_{service}.ext
            basename = os.path.basename(file_path)
            name_no_ext = os.path.splitext(basename)[0]
            # Split on last underscore to get service
            parts = name_no_ext.rsplit("_", 1)
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
            self._note_fail(te.NOT_AVAILABLE)
            return None

        if not self._rate_limiter.is_healthy("librespot"):
            logger.warning("librespot circuit breaker open; skipping Tier 0.")
            self._note_fail(te.CIRCUIT_OPEN)
            return None

        if not track.spotify_id:
            logger.debug("Tier 0 skipped for track %d: no spotify_id", track.id)
            self._note_fail(te.NO_SOURCE_ID)
            return None

        # F4: pre-filter tracks that librespot consistently can't fetch.
        # Upstream issue #318 (RuntimeError: Cannot get alternative track) fires
        # 100% of the time on tracks that Spotify has no playable variant for in
        # the account's region/format — most commonly album skits, podcast
        # interludes, and similar non-music content.  Empirically (musicstream
        # log 2026-05-26): every "Skit"-titled track failed this way.  Skip them
        # at the top so they don't burn a librespot attempt + 30-min cooldown
        # quota.  Tier 2/4 (yt-dlp) can still find them on YouTube.
        # Override with LIBRESPOT_FILTER_UNPLAYABLE=false for tests.
        _filter_unplayable = os.environ.get("LIBRESPOT_FILTER_UNPLAYABLE", "true").lower() == "true"
        if _filter_unplayable and track.title:
            _title_lower = track.title.lower()
            # Only trigger on whole-word matches — "intronaut" should NOT match "intro".
            import re as _re
            _unplayable_patterns = (r"\bskit\b", r"\binterlude\b")
            if any(_re.search(p, _title_lower) for p in _unplayable_patterns):
                logger.info(
                    "Tier 0 pre-skip for track %d ('%s'): title matches non-music heuristic",
                    track.id, track.title,
                )
                self._note_fail(te.NONMUSIC_SKIP)
                return None

        out_dir: Optional[str] = None
        try:
            _librespot_auth_ok = False
            with _LIBRESPOT_SEMAPHORE:
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
                    self._note_fail(te.EMPTY_STREAM)
                    return None

            # FFmpeg conversion is CPU-bound, not librespot-session-bound — release
            # the semaphore here so the next worker can start streaming while this
            # one transcodes.
            # Convert OGG → MP3 320k
            mp3_in_workdir = ogg_path.replace(".ogg", ".mp3")
            import subprocess as _sp
            ffmpeg_result = _sp.run(
                [
                    "ffmpeg", "-i", ogg_path,
                    "-codec:a", "libmp3lame", "-b:a", "320k",
                    "-y", "-loglevel", "error",
                    mp3_in_workdir,
                ],
                capture_output=True,
                timeout=120,
            )
            try:
                os.remove(ogg_path)
            except OSError:
                pass

            mp3_size = os.path.getsize(mp3_in_workdir) if os.path.exists(mp3_in_workdir) else 0
            if ffmpeg_result.returncode != 0 or mp3_size < 10_240:
                logger.warning(
                    "librespot: ffmpeg conversion failed for track %d (size=%d): %s",
                    track.id, mp3_size, ffmpeg_result.stderr.decode(errors="replace")[:200],
                )
                self._rate_limiter.record_failure("librespot")
                self._note_fail(te.FFMPEG_FAIL)
                return None

            # Promote MP3 OUT of the per-call workdir so the workdir can be
            # cleaned in the finally block. Caller will move/rename this path
            # again in the organiser stage.
            mp3_path = os.path.join(TEMP_DIR, f"librespot_{uuid.uuid4().hex}.mp3")
            shutil.move(mp3_in_workdir, mp3_path)

            self._rate_limiter.record_success("librespot")
            logger.info("librespot: track %d ('%s') downloaded OK", track.id, track.title)
            return mp3_path

        except Exception as exc:
            # Distinguish between (a) genuine librespot/auth failures and (b)
            # "Cannot get alternative track" — a benign upstream-issue-#318
            # signal that THIS specific track has no playable variant for our
            # account/region. Treating (b) as a regular failure poisons the
            # circuit breaker (5 in a row → 30 min cooldown that locks tier 0
            # for unrelated tracks) and floods the log with WARNINGs that
            # aren't actually actionable. The track will cascade to tiers 1-5
            # cleanly the same way our F4 pre-skip path handles it.
            exc_str = str(exc).lower()
            is_alt_track_signal = "cannot get alternative track" in exc_str
            is_auth_failure = not _librespot_auth_ok or any(
                kw in exc_str for kw in ("auth", "credential", "login", "token", "unauthorized", "403", "invalid")
            )

            if is_alt_track_signal:
                # Log at INFO and DO NOT count against the circuit breaker.
                # We DO still log it — operators want visibility into how many
                # tracks are hitting this so they can correlate with Spotify
                # catalogue changes — just not at WARN/ERROR severity.
                logger.info(
                    "librespot: no playable variant for track %d ('%s') — cascading to next tier",
                    track.id, track.title,
                )
                self._note_fail(te.REGION_UNAVAIL)
                return None

            self._rate_limiter.record_failure("librespot")
            logger.warning("librespot failed for track %d ('%s'): %s", track.id, track.title, exc)
            # Only invalidate the session on auth failures or explicit auth signals.
            # Transient stream errors (IOError, empty chunk, decode error) don't
            # mean the session is dead — nuking it on every blip forces expensive
            # full re-auth on the next call.
            if is_auth_failure:
                global _librespot_session
                _librespot_session = None
            self._note_fail(te.AUTH_FAILURE if is_auth_failure else te.STREAM_ERROR)
            return None
        finally:
            # Always remove the per-call workdir. Success path already moved
            # the MP3 out of it; failure path leaves nothing worth keeping.
            if out_dir is not None:
                shutil.rmtree(out_dir, ignore_errors=True)

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

        # OFFICIAL_SOURCE_FILTER_V1: collect candidates with metadata across
        # filters, score via _score_youtube_candidate, sort by score desc.
        scored_candidates: list[tuple[int, str]] = []
        seen_vids: set[str] = set()
        for search_filter in ("songs", "videos"):
            try:
                ytm = YTMusic()
                results = ytm.search(query=query, filter=search_filter, limit=4)
                for result in results:
                    vid = result.get("videoId")
                    if not vid or vid in seen_vids:
                        continue
                    seen_vids.add(vid)
                    # Build an info-shaped dict for the scorer
                    artists_field = result.get("artists") or []
                    primary_channel = artists_field[0].get("name") if artists_field else ""
                    duration_seconds = None
                    dur = result.get("duration_seconds") or result.get("duration")
                    if isinstance(dur, int):
                        duration_seconds = dur
                    elif isinstance(dur, str) and ":" in dur:
                        # mm:ss or h:mm:ss
                        parts = [int(p) for p in dur.split(":") if p.isdigit()]
                        if len(parts) == 2:
                            duration_seconds = parts[0] * 60 + parts[1]
                        elif len(parts) == 3:
                            duration_seconds = parts[0] * 3600 + parts[1] * 60 + parts[2]
                    info_shape = {
                        "title": result.get("title") or "",
                        "channel": primary_channel,
                        "uploader": primary_channel,
                        "duration": duration_seconds,
                    }
                    score = self._score_youtube_candidate(info_shape, track)
                    # Songs filter implicitly ARE official audio on YT Music;
                    # bonus +50 to prefer them over the videos filter
                    if search_filter == "songs":
                        score += 50
                    scored_candidates.append((score, vid))
            except Exception as exc:
                self._rate_limiter.record_failure("ytmusicapi")
                logger.warning("ytmusicapi search (filter=%s) failed: %s", search_filter, exc)

        if not scored_candidates:
            return None

        # Sort by score desc, drop negatives
        scored_candidates.sort(key=lambda kv: kv[0], reverse=True)
        if scored_candidates and scored_candidates[0][0] < 0:
            logger.info(
                "Tier 2: no official-grade candidate for track %d (best score=%d) — skipping tier",
                track.id, scored_candidates[0][0],
            )
            return None
        candidates = [vid for score, vid in scored_candidates if score >= 0]
        logger.info(
            "Tier 2 [official-filter] track %d: %d candidates, top score=%d",
            track.id, len(candidates), scored_candidates[0][0],
        )

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

        spotdl_mode = os.environ.get("SPOTDL_MODE", "cli").lower()
        if spotdl_mode == "http":
            service_url = os.environ.get("SPOTDL_SERVICE_URL", "")
            if not service_url:
                logger.warning("SPOTDL_SERVICE_URL not set; falling back to CLI mode")
                spotdl_mode = "cli"
            else:
                try:
                    import requests
                    logger.debug("Running spotdl (HTTP) for track %d", track.id)
                    resp = requests.post(
                        f"{service_url.rstrip('/')}/download",
                        json={"spotify_uri": track.spotify_uri},
                        timeout=120
                    )
                    if resp.status_code == 200:
                        out_file = os.path.join(TEMP_DIR, f"spotdl_{track.id}_{uuid.uuid4().hex[:8]}.mp3")
                        with open(out_file, "wb") as f:
                            f.write(resp.content)
                        self._rate_limiter.record_success("spotdl")
                        self._throttle.on_success("spotdl")
                        logger.info("spotdl HTTP successfully downloaded track %d", track.id)
                        return out_file
                    else:
                        logger.warning("spotdl HTTP failed: %s - %s", resp.status_code, resp.text[:200])
                except requests.exceptions.Timeout:
                    logger.warning("spotdl HTTP timeout for track %d", track.id)
                    self._rate_limiter.record_failure("spotdl")
                    return None
                except Exception as exc:
                    logger.warning("spotdl HTTP error: %s", exc)
                    self._rate_limiter.record_failure("spotdl")
                    # Fall back to CLI if HTTP is broken? The spec says CLI fallback always available, meaning if mode=cli or HTTP fails, we could fallback, or maybe just if not configured. 
                    # Let's fallback to CLI for the PoC if HTTP fails.
                    logger.info("Falling back to spotdl CLI for track %d", track.id)
                    spotdl_mode = "cli"

        if spotdl_mode == "cli":
            out_dir: Optional[str] = None
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

                # ── Secret hygiene ────────────────────────────────────────────────
                # SPEC §B / audit #7: passing --client-id / --client-secret on argv
                # means the credentials are visible to any local user via `ps aux`
                # / `/proc/<pid>/cmdline` / Docker monitoring sidecars. spotdl
                # honours the SPOTIPY_CLIENT_ID / SPOTIPY_CLIENT_SECRET env vars
                # (pinned to the underlying spotipy library). We pass via the
                # subprocess `env=` arg so the secrets cross only the parent →
                # child process boundary as kernel-private memory, never argv.
                spotdl_env = {**os.environ,
                              "SPOTIPY_CLIENT_ID": client_id,
                              "SPOTIPY_CLIENT_SECRET": client_secret}

                # URI must come BEFORE --audio: spotdl's --audio uses nargs=* and will
                # greedily consume the URI as an audio-source value, failing argparse validation.
                base_cmd = [
                    spotdl_path,
                    spotify_uri,
                    "--output", out_dir,
                    "--format", "mp3",
                    "--bitrate", "320k",
                    "--log-level", "ERROR",
                ]

                # Attempt 1: youtube-music (Spotify-matched, most accurate)
                cmd_ytm = base_cmd + ["--audio", "youtube-music"]
                logger.debug("Running spotdl (youtube-music) for track %d", track.id)
                result = subprocess.run(
                    cmd_ytm,
                    capture_output=True,
                    text=True,
                    timeout=120,
                    env=spotdl_env,
                )

                # OFFICIAL_SOURCE_FILTER_V1: previously fell back to --audio youtube
                # which often picks lyric videos / fan uploads. Drop that fallback
                # entirely — let the track cascade to Tier 4 yt-dlp YouTube which
                # now scores candidates and rejects unofficial sources.
                if result.returncode != 0:
                    logger.info(
                        "spotdl --audio youtube-music failed for track %d ('%s'); cascading to Tier 4 (no plain youtube fallback): %s",
                        track.id, track.title, (result.stderr or "")[:120],
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

                # Promote the downloaded file OUT of out_dir so we can clean
                # the workdir without losing the result.
                ext = os.path.splitext(downloaded_file)[1]
                promoted = os.path.join(TEMP_DIR, f"spotdl_{uuid.uuid4().hex}{ext}")
                shutil.move(downloaded_file, promoted)
                shutil.rmtree(out_dir, ignore_errors=True)

                self._rate_limiter.record_success("spotdl")
                self._throttle.on_success("spotdl")
                logger.info("spotdl CLI successfully downloaded track %d via %s", track.id, os.path.basename(promoted))
                return promoted

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
            finally:
                # Always remove the per-call workdir. Success path moved the
                # promoted file out and already removed out_dir; failure paths
                # (timeout, exception, early return) reach here with out_dir
                # still on disk.
                if out_dir is not None:
                    try:
                        import shutil as _shutil
                        _shutil.rmtree(out_dir, ignore_errors=True)
                    except Exception:  # noqa: BLE001
                        pass

    # ── Tier 4: yt-dlp YouTube direct search ─────────────────────────────────

    # OFFICIAL_SOURCE_FILTER_V1: scoring rules to prefer official audio over
    # lyric videos / fan uploads / 8D / sped-up / reaction content.
    _BAD_TITLE_TOKENS = (
        "lyric", "lyrics", "(audio)", "cover", "remix", "live ", "reaction",
        "review", "tribute", "8d audio", "slowed", "sped up", "nightcore",
        "reverb", "remastered fan", "fan made", "fan-made", "guitar tutorial",
        "piano tutorial", "instrumental", "karaoke", "loop", "1 hour", "10 hours",
        "extended", "ai cover", "type beat",
    )
    _OFFICIAL_TITLE_TOKENS = (
        "official audio", "official video", "official music video", "official",
    )

    def _score_youtube_candidate(self, info: dict, track: Track) -> int:
        """OFFICIAL_SOURCE_FILTER_V1: score a yt-dlp/ytmusic candidate.

        Higher = more likely to be the official master recording. Returns a
        signed int. Caller picks the highest-scoring candidate that passes
        duration check.

        Inputs (info):
            channel / uploader / channel_url — channel identity
            title — video title
            duration — seconds
            view_count — popularity tiebreaker

        Heuristics:
            +200 channel ends in ' - Topic' (auto-gen master-recording channel)
            +150 channel contains 'VEVO' or exact artist name
            +60  title contains 'official audio' or 'official music video'
            -150 title token in _BAD_TITLE_TOKENS
                  (unless that token also appears in track.title — e.g.
                   a song literally called 'Live')
            duration delta penalty: -2 per second of |actual - expected|
        """
        score = 0

        title = (info.get("title") or "").lower()
        channel = (info.get("channel") or info.get("uploader") or "").lower()
        track_title_l = (track.title or "").lower()
        track_artist_l = (track.artist or "").lower()

        # Channel: Topic and VEVO are the gold standard
        if channel.endswith(" - topic"):
            score += 200
        if "vevo" in channel:
            score += 150
        # Exact artist channel name match (cleaned)
        if track_artist_l and track_artist_l in channel:
            score += 80

        # Title: official markers
        for tok in self._OFFICIAL_TITLE_TOKENS:
            if tok in title:
                score += 60
                break

        # Title: bad tokens — only penalise if NOT in original track title
        for bad in self._BAD_TITLE_TOKENS:
            if bad in title and bad not in track_title_l:
                score -= 150

        # Duration penalty
        if track.duration_ms is not None:
            expected_s = track.duration_ms / 1000.0
            got_s = info.get("duration") or 0
            if got_s:
                delta = abs(got_s - expected_s)
                score -= int(delta * 2)
                if delta > _DURATION_TOLERANCE_S:
                    score -= 200  # hard penalty over tolerance

        return score

    def _tier4_ytdlp_youtube(self, track: Track) -> Optional[str]:
        """
        Search YouTube with ytsearch12 using two query variants:
          - "{title} {artist} audio"
          - "{title} {artist} official audio"
        Returns temp file path (MP3 320kbps) or None.

        OFFICIAL_SOURCE_FILTER_V1: instead of downloading the first hit,
        flat-extract all results, score each via _score_youtube_candidate,
        download the highest-scoring candidate (must score >= 0).
        """
        if not self._rate_limiter.is_healthy("youtube"):
            logger.warning("YouTube circuit breaker open; skipping Tier 4.")
            return None

        queries = [
            f"ytsearch12:{track.title} {track.artist} audio",
            f"ytsearch12:{track.title} {track.artist} official audio",
        ]

        # OFFICIAL_SOURCE_FILTER_V1: collect candidates across queries, score, sort.
        candidates: list[dict] = []
        seen_ids: set[str] = set()
        for query in queries:
            if not self._throttle.wait("youtube"):
                logger.info("Throttle skip: youtube tier 4 track %d — will retry next run", track.id)
                return None
            flat_opts = {
                "quiet": True,
                "no_warnings": True,
                "skip_download": True,
                "extract_flat": "in_playlist",
                "noplaylist": False,
                "ignoreerrors": True,
            }
            if os.path.exists("cookies.txt") and os.path.getsize("cookies.txt") > 0:
                flat_opts["cookiefile"] = "cookies.txt"
            try:
                with yt_dlp.YoutubeDL(flat_opts) as ydl:
                    flat = ydl.extract_info(query, download=False)
            except Exception as exc:
                if self._is_youtube_session_rate_limited(exc):
                    self._rate_limiter.force_open("youtube", "session rate-limited")
                    self._throttle.on_rate_limit("youtube")
                    logger.warning("YouTube session rate-limited (flat); CB opened")
                    return None
                logger.warning("Tier 4 flat-extract '%s' failed: %s", query, exc)
                continue
            for entry in (flat or {}).get("entries") or []:
                if not entry:
                    continue
                vid = entry.get("id") or entry.get("url")
                if not vid or vid in seen_ids:
                    continue
                seen_ids.add(vid)
                candidates.append(entry)

        if not candidates:
            logger.info("Tier 4: no candidates from flat-extract for track %d", track.id)
            return None

        # Score and sort
        scored = [(self._score_youtube_candidate(c, track), c) for c in candidates]
        scored.sort(key=lambda kv: kv[0], reverse=True)
        # Log top-3 for debug visibility
        if scored:
            top3 = scored[:3]
            logger.info(
                "Tier 4 [official-filter] track %d ('%s' / '%s'): top candidates = %s",
                track.id, track.title, track.artist,
                [(s, (c.get("title") or "")[:50], (c.get("channel") or c.get("uploader") or "")[:30]) for s, c in top3],
            )

        # Try in score order; reject anything with score < 0 (hard fail)
        for score, cand in scored:
            if score < 0:
                logger.info(
                    "Tier 4: no official-grade candidate for track %d (best score=%d) — giving up tier",
                    track.id, scored[0][0] if scored else -999,
                )
                return None

            video_id = cand.get("id") or cand.get("url")
            if not video_id:
                continue
            url = f"https://www.youtube.com/watch?v={video_id}"

            if not self._throttle.wait("youtube"):
                logger.info("Throttle skip: youtube tier 4 track %d (score-loop) — will retry next run", track.id)
                return None

            out_stem = os.path.join(TEMP_DIR, str(uuid.uuid4()))
            ydl_opts = self._build_mp3_opts(out_stem)
            try:
                with yt_dlp.YoutubeDL(ydl_opts) as ydl:
                    ydl.download([url])
            except Exception as exc:
                if self._is_youtube_session_rate_limited(exc):
                    self._rate_limiter.force_open("youtube", "session rate-limited")
                    self._throttle.on_rate_limit("youtube")
                    logger.warning("YouTube session rate-limited; CB opened, stopping Tier 4")
                    return None
                if not self._is_content_error(exc):
                    self._rate_limiter.record_failure("youtube")
                logger.warning("Tier 4 download '%s' failed: %s", url, exc)
                continue

            downloaded = self._find_output_file(out_stem)
            if downloaded and os.path.exists(downloaded) and os.path.getsize(downloaded) > 0:
                self._rate_limiter.record_success("youtube")
                self._throttle.on_success("youtube")
                logger.info(
                    "Tier 4: track %d delivered via official-filtered candidate (score=%d, vid=%s)",
                    track.id, score, video_id,
                )
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

    def _note_fail(self, reason: str) -> None:
        """Record (thread-locally) WHY the current tier is about to return None.

        The tier loop consumes this in the _record_attempt call so
        download_attempts.error holds the real reason (e.g. region_unavailable)
        instead of a placeholder. threading.local keeps the concurrent download
        workers isolated from each other.
        """
        self._fail_tls.fail_reason = reason

    def _record_librespot_timeout(self, track_id: int) -> None:
        """Record a librespot per-track timeout as a rate_limited attempt.

        Timeouts are the symptom of Spotify per-account rate limiting (a hung
        C-level stream.read until the 90s watchdog fires). The worker's session
        is racy to write from here, so we use an INDEPENDENT session that never
        touches it (Oracle review). attempt_count is bumped to keep give-up
        logic accurate; this may rarely double-count if the worker's own
        _record_attempt commits before the sweep closes its session - accepted,
        since a systematic undercount on timeouts is worse than a rare overcount.
        """
        from src.db import get_session
        try:
            with get_session() as rec:
                self._record_attempt(
                    rec, track_id, "tier0_librespot",
                    error=te.RATE_LIMITED, success=False,
                )
                rt = rec.get(Track, track_id)
                if rt is not None:
                    rt.attempt_count = (rt.attempt_count or 0) + 1
                    rt.last_attempt_at = _utcnow()
        except Exception as exc:  # noqa: BLE001 - recording must never break the sweep
            logger.debug(
                "could not record librespot timeout attempt for %d: %s", track_id, exc,
            )

    # ── Give-up logic ──────────────────────────────────────────────────────────

    def _should_give_up(self, session: Session, track_id: int) -> bool:
        """Return True once the track has >= _GIVE_UP_THRESHOLD failed attempts (P2-6).

        Reads the maintained tracks.attempt_count column (migration 0003,
        incremented on every failed tier attempt and backfilled from
        download_attempts) instead of COUNT(download_attempts) on every check —
        equivalent semantics, cheaper. The track is in this session's identity
        map, so .get() returns the in-memory object with this run's increments.
        """
        track = session.get(Track, track_id)
        return bool(track) and (track.attempt_count or 0) >= _GIVE_UP_THRESHOLD

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
                # on open, causing EROFS. Reuse a single per-instance temp
                # copy instead of leaking a fresh tempfile per call.
                # (audit #20)
                tmp_path = self._get_or_refresh_cookie_copy(cookies_src)
                if tmp_path:
                    opts["cookiefile"] = tmp_path

        return opts

    def _find_output_file(self, out_stem: str) -> Optional[str]:
        """Find the downloaded file matching the stem (any audio extension)."""
        for ext in ("mp3", "m4a", "opus", "webm", "flac", "ogg"):
            candidate = out_stem + f".{ext}"
            if os.path.exists(candidate):
                return candidate
        return None

    def _get_or_refresh_cookie_copy(self, cookies_src: str) -> Optional[str]:
        """Return a writable copy of cookies.txt, refreshed when source mtime moves.

        We keep one copy per Downloader instance and refresh only when the
        underlying file changes — Docker bind-mount cookies are typically
        rotated on a daily basis, much rarer than the per-call frequency
        the previous implementation triggered.

        Old copies (with stale mtime) get unlinked. Any errors degrade
        gracefully to "no cookies" instead of breaking the tier.
        """
        try:
            src_mtime = os.path.getmtime(cookies_src)
        except OSError:
            return None

        cached = getattr(self, "_active_cookie_copy", None)
        if cached and cached.get("src_mtime") == src_mtime and os.path.exists(cached["path"]):
            return cached["path"]

        # Refresh: unlink old copy, create new one.
        if cached and os.path.exists(cached.get("path", "")):
            try:
                os.unlink(cached["path"])
                self._tmp_cookie_files.discard(cached["path"])
            except OSError:
                pass

        try:
            tmp = tempfile.NamedTemporaryFile(suffix=".txt", delete=False, dir=TEMP_DIR)
            shutil.copy2(cookies_src, tmp.name)
            tmp.close()
        except Exception as exc:  # noqa: BLE001
            logger.debug("Could not copy cookies.txt to temp: %s", exc)
            return None

        self._tmp_cookie_files.add(tmp.name)
        self._active_cookie_copy = {"path": tmp.name, "src_mtime": src_mtime}
        return tmp.name

    def cleanup_temp_cookies(self) -> None:
        """Remove any tracked temp cookie copies. Safe to call repeatedly."""
        for path in list(self._tmp_cookie_files):
            try:
                if os.path.exists(path):
                    os.unlink(path)
            except OSError as exc:
                logger.debug("Could not remove temp cookies %r: %s", path, exc)
            finally:
                self._tmp_cookie_files.discard(path)
        self._active_cookie_copy = None

    def __del__(self) -> None:
        # Best-effort cleanup at GC time. __del__ is unreliable so we don't
        # depend on it for correctness — the daemon shutdown hook should
        # call cleanup_temp_cookies() explicitly.
        try:
            self.cleanup_temp_cookies()
        except Exception:  # noqa: BLE001
            pass

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

    def download_batch_spotdl(self, session, tracks: list[Track]) -> None:
        """T9: spotdl batch PoC (CLI array mode)
        Downloads a batch of tracks using spotdl's multi-URL support.
        Maps files back to tracks using {track_id}.{ext} as output template to satisfy V5.
        """
        if not tracks:
            return
            
        import subprocess
        import shutil
        import uuid
        from src.models import TrackStatus
        
        spotdl_path = shutil.which("spotdl")
        if not spotdl_path:
            logger.warning("spotdl CLI not found; skipping batch PoC.")
            return
            
        batch_id = uuid.uuid4().hex[:8]
        out_dir = os.path.join(TEMP_DIR, f"spotdl_batch_{batch_id}")
        os.makedirs(out_dir, exist_ok=True)
        
        uris = [t.spotify_uri for t in tracks if t.spotify_uri]
        if not uris:
            return
            
        cmd = [spotdl_path] + uris + [
            "--output-format", "mp3",
            "--output", out_dir,
            "-p", "{track_id}.{ext}"
        ]
        
        logger.info("Starting spotdl batch download for %d tracks", len(uris))
        
        try:
            client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
            client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "")
            spotdl_env = {**os.environ,
                          "SPOTIPY_CLIENT_ID": client_id,
                          "SPOTIPY_CLIENT_SECRET": client_secret}
                          
            subprocess.run(cmd, env=spotdl_env, check=False)
            
            for track in tracks:
                if not track.spotify_uri:
                    continue
                track_id = track.spotify_uri.split(":")[-1]
                expected_file = os.path.join(out_dir, f"{track_id}.mp3")
                
                if os.path.exists(expected_file):
                    logger.info("Batch spotdl downloaded track %d", track.id)
                    try:
                        self._tagger.tag_file(expected_file, track)
                        final_path = self._organiser.organise(expected_file, track)
                        
                        track.file_path = final_path
                        track.status = TrackStatus.DOWNLOADED.value
                        track.download_method = "spotdl_batch"
                        session.commit()
                    except Exception as e:
                        logger.error("Error organizing batch track %d: %s", track.id, e)
                else:
                    logger.warning("Batch spotdl missing file for track %d", track.id)
                    
        except Exception as e:
            logger.error("Batch spotdl PoC error: %s", e)
        finally:
            try:
                shutil.rmtree(out_dir, ignore_errors=True)
            except Exception:
                pass
