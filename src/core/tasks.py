import logging
import os
import subprocess
import threading
from datetime import datetime, timezone
from functools import wraps
from typing import Optional, Callable, Any

from src.core.config import LOG_DIR, BACKUP_DIR, MAX_BACKUPS, DISABLE_DOWNLOADS, SPOTIFY_CLIENT_ID

logger = logging.getLogger("musicstream.daemon")

# ── Resilience Helpers ────────────────────────────────────────────────────────

_SPOTIFY_TASK_LOCK = threading.Lock()


def _spotify_task_min_token_hours() -> float:
    try:
        return max(0.0, float(os.environ.get("SPOTIFY_TASK_MIN_TOKEN_HOURS", "0.5")))
    except ValueError:
        return 0.5


def _int_env(name: str, default: int, *, minimum: int = 0) -> int:
    try:
        return max(minimum, int(os.environ.get(name, str(default))))
    except ValueError:
        return default


def stale_download_minutes() -> int:
    return _int_env("STALE_DOWNLOAD_MINUTES", 30, minimum=1)


def download_progress_max_stale_hours() -> float:
    try:
        return max(0.0, float(os.environ.get("DOWNLOAD_PROGRESS_MAX_STALE_HOURS", "6")))
    except ValueError:
        return 6.0


def download_progress_startup_grace_seconds() -> int:
    return _int_env("DOWNLOAD_PROGRESS_STARTUP_GRACE_S", 1800, minimum=0)


def _as_utc(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _run_spotify_task(task_name: str, work: Callable[[], Any]) -> Any:
    """Run one Spotify task at a time and refresh a stale cache first."""
    if not _SPOTIFY_TASK_LOCK.acquire(blocking=False):
        logger.warning("%s skipped: another Spotify task is already running.", task_name)
        return None
    try:
        refresh_spotify_token_if_expired(max_age_hours=_spotify_task_min_token_hours())
        return work()
    finally:
        _SPOTIFY_TASK_LOCK.release()


def pause_on_no_internet(func: Callable) -> Callable:
    """Decorator that blocks execution until internet is available."""
    @wraps(func)
    def wrapper(*args, **kwargs) -> Any:
        from src.utils import wait_for_internet
        wait_for_internet()
        return func(*args, **kwargs)
    return wrapper

# ── Stats Helpers ─────────────────────────────────────────────────────────────

def _get_db_track_count() -> int:
    """Return the total number of tracks in the DB, or 0 on error."""
    try:
        from src.db import get_session
        from src.models import Track
        with get_session() as session:
            return session.query(Track).count()
    except Exception as exc:
        logger.warning("Could not query DB track count: %s", exc)
        return 0

def _get_last_daemon_run():
    """Return the most recent DaemonRun row, or None."""
    try:
        from src.db import get_session
        from src.models import DaemonRun
        with get_session() as session:
            return (
                session.query(DaemonRun)
                .order_by(DaemonRun.started_at.desc())
                .first()
            )
    except Exception as exc:
        logger.warning("Could not query last daemon run: %s", exc)
        return None

def _get_errors_log_size() -> float:
    """Return the size of errors.log in MB."""
    path = LOG_DIR / "errors.log"
    try:
        return path.stat().st_size / (1024 * 1024)
    except OSError:
        return 0.0

# ── Pipeline Tasks ────────────────────────────────────────────────────────────

@pause_on_no_internet
def integrity_check() -> None:
    """Run the file integrity checker and log results."""
    logger.info("Running integrity check…")
    try:
        from src.db import get_session
        from src.integrity.checker import IntegrityChecker
        checker = IntegrityChecker()
        with get_session() as session:
            result = checker.run(session)
        logger.info(
            "Integrity check complete: total=%d ok=%d missing=%d corrupt=%d",
            result.total_checked, result.ok, result.missing, result.corrupt,
        )
    except Exception as exc:
        logger.error("Integrity check failed: %s", exc, exc_info=True)

@pause_on_no_internet
def spotify_incremental_sync() -> None:
    """Run Spotify incremental sync and log new track count."""
    logger.info("Running Spotify incremental sync.")
    try:
        def _work():
            from src.db import get_session
            from src.ingestion.scraper import SpotifyScraper
            scraper = SpotifyScraper(client_id=SPOTIFY_CLIENT_ID)
            with get_session() as session:
                return scraper.incremental_sync(session)

        new_tracks = _run_spotify_task("Spotify incremental sync", _work)
        if new_tracks is not None:
            logger.info("Spotify incremental sync complete: %d new tracks", new_tracks)
    except Exception as exc:
        logger.error("Spotify incremental sync failed: %s", exc, exc_info=True)


@pause_on_no_internet
def spotify_saved_albums_sync() -> None:
    """Pull every Spotify Saved Album and upsert all its tracks."""
    logger.info("Running Spotify saved-albums sync.")
    try:
        def _work():
            from src.db import get_session
            from src.ingestion.scraper import SpotifyScraper
            scraper = SpotifyScraper(client_id=SPOTIFY_CLIENT_ID)
            with get_session() as session:
                return scraper.saved_albums_sync(session)

        new_tracks = _run_spotify_task("Spotify saved-albums sync", _work)
        if new_tracks is not None:
            logger.info("Spotify saved-albums sync complete: %d new tracks", new_tracks)
    except Exception as exc:
        logger.error("Spotify saved-albums sync failed: %s", exc, exc_info=True)


@pause_on_no_internet
def spotify_followed_artists_sync() -> None:
    """Pull every followed artist's full discography (heavy weekly sweep)."""
    logger.info("Running Spotify followed-artists sync.")
    try:
        def _work():
            from src.db import get_session
            from src.ingestion.scraper import SpotifyScraper
            scraper = SpotifyScraper(client_id=SPOTIFY_CLIENT_ID)
            with get_session() as session:
                return scraper.followed_artists_sync(session)

        new_tracks = _run_spotify_task("Spotify followed-artists sync", _work)
        if new_tracks is not None:
            logger.info("Spotify followed-artists sync complete: %d new tracks", new_tracks)
    except Exception as exc:
        logger.error("Spotify followed-artists sync failed: %s", exc, exc_info=True)


@pause_on_no_internet
def spotify_liked_artists_expand(batch_size: int = 50) -> None:
    """LIKED_ARTISTS_EXPAND_V1: discography-expand artists from Liked Songs +
    Saved Albums, including appears_on (guest features). Bounded daily job —
    chips away at the long tail without bursting the Spotify rate limiter."""
    logger.info("Running Spotify liked-artists expand (batch=%d).", batch_size)
    try:
        def _work():
            from src.db import get_session
            from src.ingestion.scraper import SpotifyScraper
            scraper = SpotifyScraper(client_id=SPOTIFY_CLIENT_ID)
            with get_session() as session:
                return scraper.liked_artists_expand(session, batch_size=batch_size)

        result = _run_spotify_task("Spotify liked-artists expand", _work)
        if result is not None:
            logger.info(
                "Spotify liked-artists expand complete: %d artists, %d new tracks, %d remaining",
                result.get("artists_expanded", 0),
                result.get("new_tracks", 0),
                result.get("remaining", 0),
            )
    except Exception as exc:
        logger.error("Spotify liked-artists expand failed: %s", exc, exc_info=True)


@pause_on_no_internet
def maybe_run_full_backfill() -> int:
    """One-time catch-up: if the DB has no album/artist sources yet, run a full
    backfill so saved-albums and followed-artists data lands. Idempotent — once
    the sources exist, this is a no-op on every subsequent boot.

    Returns the number of new tracks ingested (0 if skipped).
    """
    try:
        from src.db import get_session
        from src.models import Source, SourceType

        with get_session() as session:
            existing = (
                session.query(Source)
                .filter(Source.source_type.in_([SourceType.ALBUM.value, SourceType.ARTIST.value]))
                .count()
            )
        if existing > 0:
            logger.info("Full backfill skip: %d album/artist sources already exist", existing)
            return 0

        logger.info("Full backfill: no album/artist sources yet — running one-time catch-up")

        def _work():
            from src.ingestion.scraper import SpotifyScraper
            scraper = SpotifyScraper(client_id=SPOTIFY_CLIENT_ID)
            with get_session() as session:
                return scraper.full_backfill(session)

        new_tracks = _run_spotify_task("Spotify full backfill", _work)
        if new_tracks is None:
            return 0
        logger.info("Full backfill complete: %d new tracks", new_tracks)
        return new_tracks
    except Exception as exc:
        logger.error("Full backfill failed (non-fatal): %s", exc, exc_info=True)
        return 0

def reset_orphaned_downloads(all_rows: bool = False, stale_after_minutes: int = 30) -> int:
    """Reset stranded status='downloading' rows back to 'pending'. (P0-1)

    Rows strand in DOWNLOADING when the process dies mid-sweep: phases 1
    (librespot) and 3 (spotdl) of download_pipeline() claim rows outside the
    Phase-2 reset in DownloadOrchestrator.download_pending(), so a hard restart
    leaks queue slots permanently.

    all_rows=True  - reset EVERY downloading row (boot path). Safe because
                     --workers 1 guarantees no download worker survives a
                     process restart, so nothing is genuinely in flight.
    all_rows=False - only reset rows whose updated_at is older than
                     ``stale_after_minutes`` (mid-run path). A scheduled run
                     overlapping a still-running pipeline must not yank rows
                     from under live workers, which bump updated_at on every
                     tier transition, so an old timestamp is provably crashed.

    Returns the number of rows reset.
    """
    from datetime import timedelta
    from sqlalchemy import or_
    from src.db import get_session
    from src.models import Track, TrackStatus
    try:
        with get_session() as session:
            q = session.query(Track).filter(Track.status == TrackStatus.DOWNLOADING.value)
            if not all_rows:
                cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_after_minutes)
                q = q.filter(
                    or_(
                        Track.heartbeat_at < cutoff,
                        (Track.heartbeat_at.is_(None)) & (Track.updated_at < cutoff),
                    )
                )
            count = q.update(
                {
                    "status": TrackStatus.PENDING.value,
                    "claimed_at": None,
                    "heartbeat_at": None,
                    "claim_owner": None,
                    "daemon_run_id": None,
                },
                synchronize_session=False,
            )
            session.commit()
        if count:
            logger.info(
                "reset_orphaned_downloads: %d stranded DOWNLOADING row(s) -> PENDING (all_rows=%s)",
                count, all_rows,
            )
        return count
    except Exception as exc:
        logger.error("reset_orphaned_downloads failed: %s", exc, exc_info=True)
        return 0


def requeue_stale_downloads(stale_after_minutes: Optional[int] = None) -> int:
    """Runtime watchdog for stuck DOWNLOADING rows.

    Unlike the boot-time all-row reset, this only requeues rows whose heartbeat
    or updated_at timestamp has gone stale, so an active worker keeps its claim.
    """
    minutes = stale_after_minutes if stale_after_minutes is not None else stale_download_minutes()
    count = reset_orphaned_downloads(all_rows=False, stale_after_minutes=minutes)
    if count:
        logger.warning("Stale download watchdog requeued %d stuck track(s).", count)
        try:
            from src.services.notify import notify_failure
            notify_failure(
                "Stale downloads requeued",
                detail=f"{count} downloading track(s) had no heartbeat for {minutes}+ minutes.",
            )
        except Exception as exc:
            logger.debug("stale-download webhook skipped: %s", exc)
    return count


def get_download_liveness(
    *,
    stale_after_minutes: Optional[int] = None,
    max_stale_hours: Optional[float] = None,
    startup_grace_seconds: Optional[int] = None,
    daemon_uptime_seconds: Optional[float] = None,
) -> dict:
    """Return download progress liveness from PostgreSQL state.

    Degraded means: downloads are enabled, there is pending work, the daemon is
    past its startup grace period, and no successful download has happened
    inside the configured freshness window. Stale DOWNLOADING claims are also
    reported independently so callers can requeue or restart.
    """
    from datetime import timedelta
    from sqlalchemy import func, or_
    from src.db import get_session
    from src.models import DownloadAttempt, Track, TrackStatus

    minutes = stale_after_minutes if stale_after_minutes is not None else stale_download_minutes()
    hours = max_stale_hours if max_stale_hours is not None else download_progress_max_stale_hours()
    grace = (
        startup_grace_seconds
        if startup_grace_seconds is not None
        else download_progress_startup_grace_seconds()
    )
    uptime = daemon_uptime_seconds if daemon_uptime_seconds is not None else grace + 1

    now = datetime.now(timezone.utc)
    stale_cutoff = now - timedelta(minutes=minutes)
    success_cutoff = now - timedelta(hours=hours)

    with get_session() as session:
        pending = session.query(func.count(Track.id)).filter(
            Track.status == TrackStatus.PENDING.value
        ).scalar() or 0
        downloading = session.query(func.count(Track.id)).filter(
            Track.status == TrackStatus.DOWNLOADING.value
        ).scalar() or 0
        stale_downloading = session.query(func.count(Track.id)).filter(
            Track.status == TrackStatus.DOWNLOADING.value,
            or_(
                Track.heartbeat_at < stale_cutoff,
                (Track.heartbeat_at.is_(None)) & (Track.updated_at < stale_cutoff),
            ),
        ).scalar() or 0
        success_1h = session.query(func.count(DownloadAttempt.id)).filter(
            DownloadAttempt.success.is_(True),
            DownloadAttempt.attempted_at > now - timedelta(hours=1),
        ).scalar() or 0
        success_24h = session.query(func.count(DownloadAttempt.id)).filter(
            DownloadAttempt.success.is_(True),
            DownloadAttempt.attempted_at > now - timedelta(hours=24),
        ).scalar() or 0
        last_success_at = session.query(func.max(DownloadAttempt.attempted_at)).filter(
            DownloadAttempt.success.is_(True)
        ).scalar()

    last_success_at = _as_utc(last_success_at)
    last_success_age_seconds = None
    if last_success_at is not None:
        last_success_age_seconds = max(0, int((now - last_success_at).total_seconds()))

    past_startup_grace = uptime >= grace
    progress_fresh = True
    if not DISABLE_DOWNLOADS and pending > 0 and past_startup_grace:
        progress_fresh = last_success_at is not None and last_success_at >= success_cutoff

    return {
        "downloads_disabled": bool(DISABLE_DOWNLOADS),
        "pending": int(pending),
        "downloading": int(downloading),
        "stale_downloading": int(stale_downloading),
        "stale_download_minutes": int(minutes),
        "success_1h": int(success_1h),
        "success_24h": int(success_24h),
        "last_success_at": last_success_at.isoformat() if last_success_at else None,
        "last_success_age_seconds": last_success_age_seconds,
        "progress_fresh": bool(progress_fresh),
        "progress_max_stale_hours": float(hours),
        "startup_grace_seconds": int(grace),
        "past_startup_grace": bool(past_startup_grace),
    }


def reset_failed_tracks(session) -> int:
    """Requeue failed / failed_validation / timed_out tracks to PENDING for a
    fresh download cycle (POST /tracks/reset-failed).

    CRITICAL: also clears attempt_count + last_attempt_at. A track only reaches
    'failed' by hitting _GIVE_UP_THRESHOLD, so resetting status alone lets
    _should_give_up() re-fail it on the first tier miss without a real retry —
    defeating the entire point of the reset. Caller owns the transaction.
    """
    from src.models import Track, TrackStatus
    return (
        session.query(Track)
        .filter(Track.status.in_(["failed", "failed_validation", "timed_out"]))
        .filter(Track.blocked.is_(False))  # §W3 V7: blocked tracks are inert
        .update(
            {
                "status": TrackStatus.PENDING.value,
                "attempt_count": 0,
                "last_attempt_at": None,
                "claimed_at": None,
                "heartbeat_at": None,
                "claim_owner": None,
                "daemon_run_id": None,
            },
            synchronize_session=False,
        )
    )


def block_track(session, track_id: int, reason: str | None = None) -> bool:
    """Manually quarantine a track (§W3 T13/V7). Idempotent."""
    from src.models import Track
    track = session.get(Track, track_id)
    if track is None:
        return False
    track.blocked = True
    track.blocked_reason = reason or "manual"
    track.blocked_at = datetime.now(timezone.utc)
    session.flush()
    return True


def unblock_track(session, track_id: int) -> bool:
    """Release a quarantined track back into the pipeline (§W3 T13/V7).

    Gives the track a fresh start: status -> PENDING and download accounting
    cleared, mirroring reset_failed_tracks semantics for a single row.
    """
    from src.models import Track, TrackStatus
    track = session.get(Track, track_id)
    if track is None:
        return False
    track.blocked = False
    track.blocked_reason = None
    track.blocked_at = None
    track.status = TrackStatus.PENDING.value
    track.attempt_count = 0
    track.last_attempt_at = None
    track.claimed_at = None
    track.heartbeat_at = None
    track.claim_owner = None
    track.daemon_run_id = None
    session.flush()
    return True


def auto_block_if_exhausted(session, track) -> bool:
    """Quarantine a track once it has failed on >= AUTO_BLOCK_THRESHOLD distinct
    days (§W3 T14/V7). Distinct-day counting approximates 'consecutive full-chain
    passes' without new schema: multiple tier failures within one day collapse to
    one pass. Only non-downloaded tracks are eligible. Returns True if blocked now.
    """
    from sqlalchemy import func
    from sqlalchemy import func
    from src.core import config
    from src.models import DownloadAttempt, TrackStatus

    if track.blocked or track.status == TrackStatus.DOWNLOADED.value:
        return False

    threshold = config.AUTO_BLOCK_THRESHOLD
    rows = (
        session.query(func.date(DownloadAttempt.attempted_at))
        .filter(
            DownloadAttempt.track_id == track.id,
            DownloadAttempt.success.is_(False),
        )
        .distinct()
        .all()
    )
    # func.date() is ISO 'YYYY-MM-DD' on both SQLite and PostgreSQL.
    pass_days = len({row[0] for row in rows})
    if pass_days < threshold:
        return False

    track.blocked = True
    track.blocked_reason = f"auto: {pass_days} consecutive failed passes"
    track.blocked_at = datetime.now(timezone.utc)
    session.flush()
    logger.warning(
        "[AUTO_BLOCK] track id=%d '%s' by '%s' — %d distinct failed-pass days >= threshold %d",
        track.id, track.title, track.artist, pass_days, threshold,
    )
    try:
        from src.services.notify import notify_failure
        notify_failure(
            f"Track auto-blocked after {pass_days} failed passes",
            detail=f"{track.title} — {track.artist} (id={track.id})",
        )
    except Exception as exc:
        logger.debug("auto-block webhook skipped: %s", exc)
    return True


def _log_burn_rate() -> None:
    """P2-7: log throughput + ETA once per pipeline cycle. downloads/hr from
    successful attempts in the trailing hour, pending backlog, projected days.
    Surfaces whether the queue is converging (also exposed at /api/musicstream/burn-rate)."""
    try:
        from datetime import timedelta
        from src.db import get_session
        from src.models import Track, DownloadAttempt
        from sqlalchemy import func
        with get_session() as session:
            pending = session.query(Track).filter(Track.status == "pending").count()
            dl_1h = session.query(func.count(DownloadAttempt.id)).filter(
                DownloadAttempt.success.is_(True),
                DownloadAttempt.attempted_at > datetime.now(timezone.utc) - timedelta(hours=1),
            ).scalar() or 0
        if dl_1h > 0:
            logger.info(
                "Burn-rate: %d downloads/hr | %d pending | ETA ~%.1f days at current rate",
                dl_1h, pending, pending / dl_1h / 24.0,
            )
        else:
            logger.info("Burn-rate: 0 downloads in last hour | %d pending | ETA n/a", pending)
    except Exception as exc:
        logger.warning("Burn-rate logging failed: %s", exc)


def download_pipeline(run_id: Optional[int] = None) -> tuple[int, int]:
    """Run the download pipeline for all pending tracks. Returns (downloaded, failed)."""
    logger.info("Running download pipeline…")
    try:
        # P0-1 (defense-in-depth): clear rows stranded in DOWNLOADING by a
        # crashed prior run before claiming new work. 30-min cutoff
        # (all_rows=False) so a scheduled run overlapping a still-running
        # pipeline cannot reset rows held by live workers; the authoritative
        # all-rows reset runs once per boot in daemon._background_startup().
        reset_orphaned_downloads(all_rows=False)
        from src.db import get_session
        from src.ingestion.downloader import DownloadOrchestrator
        orchestrator = DownloadOrchestrator(daemon_run_id=run_id)

        def _librespot_phase() -> tuple[int, int]:
            try:
                with get_session() as session:
                    return orchestrator.download_pending_librespot(session)
            except Exception as exc:  # noqa: BLE001 — non-fatal; batch continues
                logger.error("librespot sweep failed (non-fatal): %s", exc, exc_info=True)
                return 0, 0

        lib_dl = 0
        lib_fail = 0
        if os.environ.get("LIBRESPOT_SWEEP_CONCURRENT", "false").lower() in ("1", "true", "yes", "on"):
            # Escape hatch for controlled experiments. Default stays serial so
            # streaming workers never overlap with the pooled downloader.
            import concurrent.futures as _cf

            with _cf.ThreadPoolExecutor(max_workers=1, thread_name_prefix="librespot-sweep") as _lib_ex:
                _lib_future = _lib_ex.submit(_librespot_phase)
                try:
                    with get_session() as session:
                        downloaded, failed = orchestrator.download_pending(session)
                except Exception as exc:  # noqa: BLE001 — non-fatal; one bad batch != dead cycle
                    logger.error("batch sweep failed (non-fatal): %s", exc, exc_info=True)
                    downloaded, failed = 0, 0
                lib_dl, lib_fail = _lib_future.result()
        else:
            lib_dl, lib_fail = _librespot_phase()
            try:
                with get_session() as session:
                    downloaded, failed = orchestrator.download_pending(session)
            except Exception as exc:  # noqa: BLE001 — non-fatal; one bad batch != dead cycle
                logger.error("batch sweep failed (non-fatal): %s", exc, exc_info=True)
                downloaded, failed = 0, 0
        logger.info(
            "Download pipeline phase complete: batch=%d librespot=%d failed=%d",
            downloaded, lib_dl, failed + lib_fail,
        )
        downloaded += lib_dl
        failed += lib_fail

        # Phase 3: spotdl sweep
        try:
            with get_session() as session:
                sdl_dl, sdl_fail = orchestrator.download_pending_spotdl(session)
            logger.info("spotdl sweep: downloaded=%d failed=%d", sdl_dl, sdl_fail)
            downloaded += sdl_dl
        except Exception as exc:
            logger.error("spotdl sweep failed (non-fatal): %s", exc, exc_info=True)
        _log_burn_rate()
        return downloaded, failed
    except Exception as exc:
        logger.error("Download pipeline failed: %s", exc, exc_info=True)
        return 0, 0

def listenbrainz_discovery() -> None:
    """Run ListenBrainz discovery and Plex playlist sync."""
    logger.info("Running ListenBrainz discovery.")
    try:
        from src.db import get_session
        from src.discovery.listenbrainz import ListenBrainzDiscovery
        from src.discovery.plex_playlists import PlexPlaylistSync
        discovery = ListenBrainzDiscovery()
        with get_session() as session:
            new_tracks = discovery.run(session)
        logger.info("ListenBrainz discovery complete: %d new tracks", new_tracks)

        if new_tracks > 0 and os.environ.get("LB_ARTIST_EXPANSION", "true").lower() in ("1", "true", "yes", "on"):
            try:
                _expand_lb_track_artists()
            except Exception as exc:
                logger.warning("LB artist-discography expansion failed (non-fatal): %s", exc)

        # §W3 T15/V8: export portable .m3u FIRST, then optional Plex push (T16).
        now = datetime.now(timezone.utc)
        month_name = now.strftime("%B")
        year = now.year
        with get_session() as session:
            try:
                from src.discovery.m3u_export import export_weekly_discovery
                out_path = export_weekly_discovery(session)
                if out_path:
                    logger.info("Weekly discovery m3u exported: %s", out_path)
            except Exception as exc:
                logger.warning("m3u weekly export failed (non-fatal, V8): %s", exc)
            plex_sync = PlexPlaylistSync()
            plex_sync.sync_discovery_playlist(session, month=month_name, year=year)
    except Exception as exc:
        logger.error("ListenBrainz discovery failed: %s", exc, exc_info=True)


def _expand_lb_track_artists(lookback_hours: int = 24, max_artists: int = 50) -> None:
    """For LB-discovered tracks (spotify_uri starts with 'mb:') ingested in the
    last *lookback_hours*, search Spotify by artist name and pull that artist's
    full discography. Skips artists who already have an ARTIST source.

    Capped at *max_artists* unique artists per run to bound API usage.
    """
    from datetime import timedelta
    from src.db import get_session
    from src.models import Track, Source, SourceType

    cutoff = datetime.now(timezone.utc) - timedelta(hours=lookback_hours)
    with get_session() as session:
        recent_lb_artists = (
            session.query(Track.artist)
            .filter(Track.spotify_uri.like("mb:%"))
            .filter(Track.created_at >= cutoff)
            .distinct()
            .limit(max_artists)
            .all()
        )
        artist_names = [row[0] for row in recent_lb_artists if row[0]]

        existing_artist_sources = {
            s.name.strip().lower() if s.name else ""
            for s in session.query(Source).filter(Source.source_type == SourceType.ARTIST.value).all()
        }
        new_artist_names = [n for n in artist_names if n.strip().lower() not in existing_artist_sources]

    if not new_artist_names:
        logger.info("LB artist expansion: nothing new to expand")
        return

    logger.info("LB artist expansion: resolving %d new artists via Spotify", len(new_artist_names))

    def _work() -> None:
        from src.ingestion.scraper import SpotifyScraper

        try:
            scraper = SpotifyScraper(client_id=SPOTIFY_CLIENT_ID)
            sp = scraper.sp
        except Exception as exc:
            logger.warning("LB artist expansion: cannot init Spotify client: %s", exc)
            return

        total_new_tracks = 0
        expanded = 0
        for name in new_artist_names:
            try:
                search_resp = sp.search(q=f'artist:"{name}"', type="artist", limit=1)
            except Exception as exc:
                logger.debug("LB artist expansion: search failed for %r: %s", name, exc)
                continue
            items = ((search_resp or {}).get("artists") or {}).get("items") or []
            if not items:
                logger.debug("LB artist expansion: no Spotify match for %r", name)
                continue
            artist = items[0]
            artist_id = artist.get("id")
            artist_name = artist.get("name") or name
            if not artist_id:
                continue
            try:
                with get_session() as session:
                    added = scraper.expand_artist_discography(session, artist_id, artist_name)
                total_new_tracks += added
                expanded += 1
            except Exception as exc:
                logger.warning("LB artist expansion: expand_artist_discography(%r) failed: %s", artist_name, exc)

        logger.info(
            "LB artist expansion complete: expanded %d/%d artists, %d new tracks queued",
            expanded, len(new_artist_names), total_new_tracks,
        )

    _run_spotify_task("ListenBrainz artist expansion", _work)

def db_backup() -> Optional[str]:
    """Run pg_dump and prune old backups."""
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_path = BACKUP_DIR / f"musicstream_{timestamp}.sql"

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        logger.error("DATABASE_URL not set; cannot run pg_dump")
        return None

    import urllib.parse as _urlparse
    _u = _urlparse.urlparse(database_url)
    _pg_env = {**os.environ, "PGPASSWORD": _u.password or ""}
    _pg_cmd = [
        "pg_dump", "-h", _u.hostname or "localhost", "-p", str(_u.port or 5432),
        "-U", _u.username or "musicstream", "-d", _u.path.lstrip("/"),
        "--no-password", "--file", str(backup_path),
    ]

    logger.info("Running pg_dump → %s", backup_path)
    try:
        result = subprocess.run(_pg_cmd, env=_pg_env, capture_output=True, text=True, timeout=300)
        if result.returncode != 0:
            logger.error("pg_dump failed (exit %d): %s", result.returncode, result.stderr[:500])
            if backup_path.exists():
                backup_path.unlink()
            return None
    except Exception as exc:
        logger.error("pg_dump error: %s", exc, exc_info=True)
        return None

    _prune_backups()
    return str(backup_path)

def _prune_backups() -> None:
    try:
        sql_files = sorted(BACKUP_DIR.glob("musicstream_*.sql"), key=lambda p: p.stat().st_mtime, reverse=True)
        for old_file in sql_files[MAX_BACKUPS:]:
            old_file.unlink()
            logger.info("Pruned old backup: %s", old_file)
    except Exception as exc:
        logger.warning("Backup pruning failed: %s", exc)

# ── Wrapper Tasks ─────────────────────────────────────────────────────────────

def full_download_pipeline() -> None:
    if DISABLE_DOWNLOADS:
        logger.info("Downloads disabled - skipping download pipeline")
        return
    run_id = _record_run_start("scheduled")
    try:
        downloaded, failed = download_pipeline(run_id=run_id)
        _record_run_complete(run_id=run_id, downloaded=downloaded, failed=failed)
    except Exception as exc:
        logger.error("Full download pipeline error: %s", exc, exc_info=True)
        _record_run_complete(run_id=run_id, notes=f"error: {exc}")

def full_integrity_check() -> None:
    integrity_check()


# ── Upgrade pass (§W3 T20/V11) ────────────────────────────────────────────

_PREMIUM_METHOD_PREFIXES = ("spotiflac_", "librespot")


def upgrade_pass(session) -> int:
    """Requeue downloaded MP3s that came from lossy tiers so the next download
    pass can try to upgrade them to cutoff quality (§W3 T20/V11).

    Single bulk UPDATE — must stay fast at 100k+ row scale. Premium sources
    (SpotiFLAC/librespot) are already at/above cutoff. Noop when
    QUALITY_CUTOFF=flac. Caller owns the transaction.
    """
    from sqlalchemy import and_, or_, select

    from src.core import config

    from src.models import Track, TrackStatus

    if config.QUALITY_CUTOFF != "mp3_320":
        return 0

    # Trickle, don't stampede: cap requeues per run (§W3 T20).
    limit = int(getattr(config, "UPGRADE_PASS_LIMIT", 500))

    not_premium = or_(
        Track.download_method.is_(None),
        Track.download_method == "",
        and_(
            ~Track.download_method.startswith("spotiflac_"),
            ~Track.download_method.startswith("librespot"),
        ),
    )
    ids = (
        session.query(Track.id)
        .filter(
            Track.status == TrackStatus.DOWNLOADED.value,
            Track.format == "mp3",
            Track.blocked.is_(False),
            not_premium,
        )
        .limit(limit)
        .subquery()
    )
    count = (
        session.query(Track)
        .filter(Track.id.in_(select(ids)))
        .update(
            {
                "status": TrackStatus.PENDING.value,
                "attempt_count": 0,
                "last_attempt_at": None,
                "claimed_at": None,
                "heartbeat_at": None,
                "claim_owner": None,
                "daemon_run_id": None,
            },
            synchronize_session=False,
        )
    )
    if count:
        logger.info("[UPGRADE_PASS] requeued %d sub-cutoff track(s)", count)
    return count

def upgrade_pass_scheduled() -> None:
    """§W3 T20 cron wrapper: requeue + let the nightly pipeline do the work."""
    try:
        from src.db import get_session
        with get_session() as session:
            count = upgrade_pass(session)
            session.commit()
        logger.info("Scheduled upgrade pass: %d track(s) requeued", count)
    except Exception as exc:
        logger.error("Scheduled upgrade pass failed: %s", exc, exc_info=True)


def discover_weekly_task() -> None:
    """§W3 T21–T23 cron wrapper: fetch → resolve → queue → export m3u."""
    try:
        from src.db import get_session
        from src.discovery.discover_weekly import DiscoverWeekly
        engine = DiscoverWeekly()
        with get_session() as session:
            summary = engine.run(session)
            session.commit()
        for pl in summary.get("playlists", []):
            logger.info(
                "[DISCOVER_WEEKLY] %s — entries=%d resolved=%d queued=%d",
                pl["name"], pl["entries"], pl["resolved_local"], pl["queued_missing"],
            )
    except Exception as exc:
        logger.error("Discover-weekly task failed: %s", exc, exc_info=True)


def update_ytdlp() -> None:
    """Daily self-heal: yt-dlp breaks against YouTube changes every few weeks.

    Stale scrapers took Tier 2/4 to 100% failure once already (2026-08-25).
    The downloader shells out to the yt-dlp CLI, so an on-disk upgrade takes
    effect on the next subprocess call — no restart needed. Runs as root in
    the container, so the system site-packages copy is updated in place.
    """
    import subprocess

    try:
        proc = subprocess.run(
            ["pip", "install", "--quiet", "--upgrade", "yt-dlp"],
            capture_output=True, timeout=600,
        )
        if proc.returncode == 0:
            logger.info("[YTDLP] auto-update OK")
        else:
            logger.warning(
                "[YTDLP] auto-update failed rc=%d: %s",
                proc.returncode, (proc.stderr or b"")[-200:],
            )
    except Exception as exc:
        logger.warning("[YTDLP] auto-update error: %s", exc)


def _spotify_token_alert_state_path():
    from pathlib import Path

    return Path(os.environ.get("SPOTIFY_TOKEN_ALERT_STATE_PATH", str(LOG_DIR / "spotify_token_alert_state.json")))


def _read_spotify_token_alert_state() -> dict:
    import json

    path = _spotify_token_alert_state_path()
    try:
        if not path.exists():
            return {}
        with path.open(encoding="utf-8") as f:
            state = json.load(f)
        return state if isinstance(state, dict) else {}
    except Exception:
        return {}


def _write_spotify_token_alert_state(state: dict) -> None:
    import json
    import tempfile

    path = _spotify_token_alert_state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(dir=str(path.parent), prefix=path.name, suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(state, f, sort_keys=True)
        os.replace(tmp_path, path)
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass


def _handle_spotify_token_probe_result(result: dict, *, context: str) -> None:
    """Alert once per degraded Spotify refresh-token episode."""
    degraded = bool(result.get("degraded"))
    state = _read_spotify_token_alert_state()
    active = bool(state.get("active"))

    if degraded:
        if active:
            return
        detail = (
            f"context={context}; present={result.get('present')}; "
            f"hours_left={result.get('hours_left')}; refreshed={result.get('refreshed')}"
        )
        try:
            from src.services.notify import notify_failure
            notify_failure("Spotify refresh token needs login", detail=detail)
        except Exception as exc:
            logger.debug("spotify-token webhook skipped: %s", exc)
        _write_spotify_token_alert_state(
            {
                "active": True,
                "first_seen_at": datetime.now(timezone.utc).isoformat(),
                "last_context": context,
            }
        )
        return

    if active:
        state["active"] = False
        state["resolved_at"] = datetime.now(timezone.utc).isoformat()
        state["last_context"] = context
        _write_spotify_token_alert_state(state)


def probe_spotify_token() -> None:
    """§W3 T18/V13 hourly early-warning: refresh-or-alert on near-expiry.

    Scheduled hourly in the daemon; also safe to call manually.
    """
    try:
        from src.ingestion.spotify_auth import probe_token
        result = probe_token(refresher=_default_token_refresher)
        _handle_spotify_token_probe_result(result, context="hourly_probe")
        if result.get("degraded"):
            logger.warning("[TOKEN_WARN] degraded token state: %s", result)
        else:
            logger.debug("Token probe healthy: %s", result)
    except Exception as exc:
        logger.warning("Spotify token probe failed: %s", exc)


def refresh_spotify_token_if_expired(max_age_hours: float = 0) -> dict:
    """Refresh the Spotify cache when missing, expired, or below a caller threshold.

    This is the startup/request-path companion to the hourly early-warning
    probe. It prevents a reboot from running Spotify sync against an expired
    cache, without refreshing on every dashboard status poll.
    """
    try:
        from src.ingestion.spotify_auth import probe_token
        result = probe_token(refresher=_default_token_refresher, max_age_hours=max_age_hours)
        _handle_spotify_token_probe_result(result, context="freshness_probe")
        if result.get("refreshed"):
            logger.info("Spotify token cache refreshed by freshness probe.")
        elif result.get("degraded"):
            logger.warning("[TOKEN_WARN] Spotify token refresh failed during freshness probe: %s", result)
        return result
    except Exception as exc:
        logger.warning("Spotify token freshness-refresh probe failed: %s", exc)
        result = {"present": False, "hours_left": None, "degraded": True, "refreshed": False}
        _handle_spotify_token_probe_result(result, context="freshness_probe_exception")
        return result


def _default_token_refresher() -> bool:
    """Silent Spotify token refresh via the token endpoint.

    The cached token may originate from EITHER a PKCE (public) or an
    authorization-code (confidential) app: Spotify rejects a secretless
    refresh for confidential clients with invalid_request/400. Send
    client_secret whenever one is configured. Rewrites the cache
    atomically and honors Spotify's optional refresh-token rotation.
    """
    import json
    import os
    import tempfile
    import time

    import requests

    client_secret = os.environ.get("SPOTIFY_CLIENT_SECRET", "").strip()

    client_id = os.environ.get("SPOTIFY_CLIENT_ID", "").strip()
    if not client_id:
        return False
    cache_path = os.environ.get("SPOTIFY_TOKEN_CACHE", "./spotify_token.json")
    if cache_path == "/app/spotify_token.json":
        cache_path = "./spotify_token.json"

    try:
        with open(cache_path, encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        return False
    refresh_token = data.get("refresh_token")
    if not refresh_token:
        return False

    try:
        payload = {
            "grant_type": "refresh_token",
            "refresh_token": refresh_token,
            "client_id": client_id,
        }
        if client_secret:  # confidential-client tokens REQUIRE the secret on refresh
            payload["client_secret"] = client_secret
        resp = requests.post(
            "https://accounts.spotify.com/api/token",
            data=payload,
            timeout=15,
        )
    except requests.RequestException:
        return False
    if resp.status_code != 200:
        logger.warning("Spotify token refresh HTTP %d", resp.status_code)
        return False

    tok = resp.json()
    data["access_token"] = tok.get("access_token")
    data["expires_at"] = int(time.time()) + int(tok.get("expires_in", 3600))
    if tok.get("scope"):
        data["scope"] = tok["scope"]
    if tok.get("refresh_token"):  # rotation - always keep the newest
        data["refresh_token"] = tok["refresh_token"]

    # Write to tmp first, then COPY IN PLACE. os.replace/atomic-rename would
    # swap the inode, which silently desynchronises single-file Docker bind
    # mounts (container keeps reading the stale pre-rename file). copyfile
    # truncates+writes the SAME inode, so host and container stay in lockstep
    # and concurrent readers never see a half-file (worst case: one partial
    # read between truncate and flush — same risk profile as spotipy's own
    # CacheFileHandler, which writes in-place too).
    fd, tmp_path = tempfile.mkstemp(dir=os.path.dirname(cache_path) or ".")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(data, f)
        import shutil
        shutil.copyfile(tmp_path, cache_path)
    except OSError:
        return False
    finally:
        try:
            os.remove(tmp_path)
        except OSError:
            pass
    return True
# ── Run Recording ─────────────────────────────────────────────────────────────

def _record_run_start(run_type: str) -> Optional[int]:
    try:
        from src.db import get_session
        from src.models import DaemonRun
        with get_session() as session:
            _close_orphaned_runs(session)
            run = DaemonRun(started_at=datetime.now(timezone.utc), run_type=run_type)
            session.add(run)
            session.flush()
            run_id = run.id
            session.commit() # Need to commit to see it in other sessions
        return run_id
    except Exception as exc:
        logger.warning("Could not record run start: %s", exc)
        return None


def _close_orphaned_runs(session, stale_after_seconds: int = 3600) -> int:
    """Mark daemon_runs with completed_at IS NULL and started_at older than
    ``stale_after_seconds`` as interrupted. Called at the start of every new run
    so observability counters stop drifting forever when the container is
    killed mid-pipeline.

    Returns the number of rows updated.
    """
    from datetime import timedelta
    from src.models import DaemonRun
    cutoff = datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
    now = datetime.now(timezone.utc)
    try:
        stale = (
            session.query(DaemonRun)
            .filter(DaemonRun.completed_at.is_(None))
            .filter(DaemonRun.started_at < cutoff)
            .all()
        )
        for run in stale:
            run.completed_at = now
            run.notes = (run.notes + " | " if run.notes else "") + "interrupted"
        if stale:
            session.flush()
            logger.info("Closed %d orphaned daemon_runs as interrupted", len(stale))
        return len(stale)
    except Exception as exc:
        logger.warning("Orphaned-run cleanup failed: %s", exc)
        return 0

def _record_run_complete(run_id: Optional[int] = None, downloaded: int = 0, failed: int = 0, scraped: int = 0, requeued: int = 0, notes: Optional[str] = None) -> None:
    try:
        from src.db import get_session
        from src.models import DaemonRun
        with get_session() as session:
            if run_id is not None:
                run = session.get(DaemonRun, run_id)
            else:
                run = session.query(DaemonRun).order_by(DaemonRun.started_at.desc()).first()
            if run:
                run.completed_at = datetime.now(timezone.utc)
                run.tracks_downloaded = downloaded
                run.tracks_failed = failed
                run.tracks_scraped = scraped
                run.tracks_requeued = requeued
                if notes:
                    run.notes = notes
                session.commit()

                # §W3 T17/V12: run summary webhook (NOTIFY_ON=all only).
                try:
                    from src.services.notify import notify_run_summary
                    notify_run_summary(
                        run_type=run.run_type,
                        downloaded=downloaded, failed=failed,
                        scraped=scraped, requeued=requeued, notes=notes,
                    )
                except Exception as exc:
                    logger.debug("run-summary webhook skipped: %s", exc)
    except Exception as exc:
        logger.warning("Could not record run completion: %s", exc)
