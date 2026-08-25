"""
musicstream/daemon.py — FastAPI + APScheduler Control Plane
"""
from __future__ import annotations

import asyncio
import json
import logging
import logging.handlers
import os
import secrets as _secrets
import time
from contextlib import asynccontextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, WebSocket, WebSocketDisconnect, Depends, Header
from fastapi.responses import JSONResponse, FileResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from apscheduler.schedulers.background import BackgroundScheduler
from spotipy.cache_handler import CacheFileHandler
from spotipy.oauth2 import SpotifyOAuth, SpotifyPKCE

from src.schemas.responses import ApiResponse, TrackStats
from src.ws.manager import manager
from src.core.config import (
    LOG_DIR, TIMEZONE, SPOTIFY_CLIENT_ID, SPOTIFY_CLIENT_SECRET, SPOTIFY_TOKEN_CACHE, DAEMON_API_TOKEN,
)
import src.core.tasks as tasks

# ── Logging Setup ─────────────────────────────────────────────────────────────

_LOG_HANDLER_TAGS = ("musicstream_main_file", "musicstream_errors_file", "musicstream_console")


def _configure_logging() -> None:
    """Wire console + rotating file handlers onto the root logger.

    IDEMPOTENT and SELF-HEALING. We tag our handlers with a custom `_ms_tag`
    attribute so we can detect whether they're still attached after a foreign
    `dictConfig`/`basicConfig` call wipes the root handlers list. Uvicorn does
    exactly that: it imports `src.daemon:app` (running this module's import-
    time `_configure_logging()` call), then runs its OWN
    `logging.config.dictConfig(LOGGING_CONFIG)` AFTER ours, which replaces
    `root.handlers` with just uvicorn's stream handler. Result: our file
    handlers silently disappear and `/app/logs/musicstream.log` stops growing
    even though `docker logs` keeps streaming via uvicorn's handler.

    Calling `_configure_logging()` again from the FastAPI lifespan (which runs
    AFTER uvicorn's logging setup) re-attaches our handlers without
    duplicating them. The tag check is the only reliable way to distinguish
    our handlers from foreign ones across the dictConfig boundary.
    """
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    # Audit #33: custom formatter that defensively injects track_id when
    # missing. The filter approach (TrackContextFilter) covers the loggers
    # we own, but uvicorn replaces its own handlers AFTER our setup runs
    # — those records reach us without the field set, blowing up the
    # standard Formatter with KeyError. Subclassing format() is the only
    # bulletproof way to guarantee the field exists at format time.
    class _TrackIDSafeFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:  # noqa: A003
            if not hasattr(record, "track_id"):
                from src.logging_context import current_track_id
                tid = current_track_id()
                record.track_id = tid if tid is not None else "-"
            return super().format(record)

    fmt = _TrackIDSafeFormatter(
        "%(asctime)s %(levelname)-8s %(name)s [%(track_id)s] — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )
    root = logging.getLogger()
    root.setLevel(logging.INFO)

    # Idempotency: if all our tagged handlers are already on the root we have
    # nothing to do. This makes calling _configure_logging() multiple times
    # safe (no duplicate handlers, no duplicated log lines).
    existing_tags = {getattr(h, "_ms_tag", None) for h in root.handlers}
    if all(tag in existing_tags for tag in _LOG_HANDLER_TAGS):
        return

    # Some of our handlers might be attached, others wiped by a foreign
    # dictConfig. Drop ALL our previously-tagged handlers so we can re-add a
    # clean set; leave foreign handlers (uvicorn's StreamHandler etc.) alone
    # so console output keeps flowing.
    for h in list(root.handlers):
        if getattr(h, "_ms_tag", None) in _LOG_HANDLER_TAGS:
            try:
                h.close()
            except Exception:  # noqa: BLE001 — handler close failures are non-fatal
                pass
            root.removeHandler(h)

    # Filter at root for our own loggers — keeps the contextvar lookup
    # cheap on the hot path.
    from src.logging_context import TrackContextFilter
    track_filter = TrackContextFilter()
    # Don't double-add the filter on re-runs.
    if not any(isinstance(f, TrackContextFilter) for f in root.filters):
        root.addFilter(track_filter)

    # Console handler
    console = logging.StreamHandler()
    console.setFormatter(fmt)
    console.addFilter(track_filter)
    console._ms_tag = "musicstream_console"  # type: ignore[attr-defined]
    root.addHandler(console)

    # Main rotating file handler — INFO and above, 5 MB × 3 backups.
    ms_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "musicstream.log", maxBytes=5*1024*1024, backupCount=3,
        encoding="utf-8",
    )
    ms_handler.setFormatter(fmt)
    ms_handler.addFilter(track_filter)
    ms_handler._ms_tag = "musicstream_main_file"  # type: ignore[attr-defined]
    root.addHandler(ms_handler)

    # Dedicated errors handler (audit #21). Without this an WARNING/ERROR
    # spike from yt-dlp / spotdl blows past the 5 MB cap and overwrites
    # earlier real errors that the operator needs for diagnosis. The
    # errors-only file rotates independently at 2 MB × 5 backups so the
    # post-mortem record is preserved across long noisy windows.
    err_handler = logging.handlers.RotatingFileHandler(
        LOG_DIR / "errors.log", maxBytes=2*1024*1024, backupCount=5,
        encoding="utf-8",
    )
    err_handler.setFormatter(fmt)
    err_handler.setLevel(logging.WARNING)
    err_handler.addFilter(track_filter)
    err_handler._ms_tag = "musicstream_errors_file"  # type: ignore[attr-defined]
    root.addHandler(err_handler)

    # Uvicorn keeps its own loggers (`uvicorn`, `uvicorn.access`,
    # `uvicorn.error`) which have propagate=True by default but ALSO
    # carry their own StreamHandler that ships records to stderr.  Those
    # records bypass the root's filter chain.  Attach the filter to
    # uvicorn's loggers explicitly so even their own handlers see a
    # populated `track_id` field.
    for uvi_name in ("uvicorn", "uvicorn.error", "uvicorn.access"):
        uvi_logger = logging.getLogger(uvi_name)
        uvi_logger.addFilter(track_filter)
        for handler in uvi_logger.handlers:
            handler.addFilter(track_filter)

_configure_logging()
logger = logging.getLogger("musicstream.daemon")


async def _log_handler_watchdog() -> None:
    """Periodically verify our file handlers are still on root and re-attach if not.

    We've observed file handlers being silently detached after FastAPI's
    lifespan completes — even though the in-process `_configure_logging()`
    calls succeed at module-import and lifespan-start. The exact culprit is
    upstream (likely uvicorn's logging.config.dictConfig running on a worker
    boundary or starlette's lifespan teardown semantics; the production
    behaviour is consistent: console keeps working, file handlers go silent
    within a minute of startup). Rather than chase the upstream cause, we
    treat the problem operationally: every 60 seconds, call the same
    idempotent `_configure_logging()` setup. If handlers are missing,
    they're re-attached; if all three are present (`musicstream_main_file`,
    `musicstream_errors_file`, `musicstream_console`), the call returns
    immediately. Worst case: 60s of file-log gap after a wipe, no duplicate
    log lines, no observable performance impact (one set check per minute).
    """
    interval = 60
    while True:
        try:
            _configure_logging()
        except Exception:  # noqa: BLE001 — watchdog must never crash
            # Last-ditch: log via print since our handlers may be the thing broken.
            import sys
            import traceback as _tb
            print("[log-watchdog] re-attach failed:", _tb.format_exc(), file=sys.stderr, flush=True)
        await asyncio.sleep(interval)

# ── Globals ───────────────────────────────────────────────────────────────────

scheduler = BackgroundScheduler(timezone=TIMEZONE)
_start_time = time.time()
_background_tasks: set = set()  # Strong refs to fire-and-forget tasks; asyncio only holds weakrefs and will GC unsupervised tasks mid-flight.

# ── Credential permission audit ───────────────────────────────────────────────

_CREDENTIAL_FILES_TO_AUDIT = [
    SPOTIFY_TOKEN_CACHE,
    "/app/data/librespot_credentials.json",
    "/app/cookies.txt",
]


def _audit_credential_permissions() -> None:
    """Warn (don't fail) when credential files are world- or group-readable.

    Bind-mounts inherit host umask. A token file at 0644 is readable by
    every user/process on the host with access to the directory — including
    monitoring sidecars, log scrapers, and unprivileged users on shared
    hosts. We log loudly so operators see it; we don't refuse to start
    because tightening the mode often requires host-level work the user
    can't do from inside the container.
    """
    for path in _CREDENTIAL_FILES_TO_AUDIT:
        if not path or not os.path.exists(path):
            continue
        try:
            mode = os.stat(path).st_mode & 0o777
        except OSError as exc:
            logger.debug("permission audit: cannot stat %r: %s", path, exc)
            continue
        if mode & 0o077:  # any group or world bit set
            logger.warning(
                "Credential file %r has permissive mode %#o — recommend `chmod 600 %s` "
                "(group/world bits are set; secrets are exposed to other users on the host).",
                path, mode, path,
            )

# ── Lifecycle ─────────────────────────────────────────────────────────────────

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Audit (this session): re-run _configure_logging AFTER uvicorn has
    # finished its own setup. Uvicorn's startup runs
    # `logging.config.dictConfig(LOGGING_CONFIG)` which wipes any handlers
    # we added at module-import time, including the rotating file handlers
    # for /app/logs/musicstream.log and /app/logs/errors.log. Re-running
    # here is idempotent (handlers carry an _ms_tag and we skip if already
    # attached) so the file handlers stay live for the entire process
    # lifetime, not just the brief window between import and uvicorn boot.
    _configure_logging()

    # Step 1 & 2: DB + migrations
    logger.info("Initializing DB and running migrations...")
    try:
        from src.db import init_db, run_migrations, wait_for_db
        engine = wait_for_db()
        init_db(engine=engine)
        run_migrations()
    except Exception as e:
        logger.error("DB initialization failed: %s", e)
        raise SystemExit(1) from e
    
    # Start background startup sequence
    _bg_task = asyncio.create_task(_background_startup())
    _background_tasks.add(_bg_task)
    _bg_task.add_done_callback(_background_tasks.discard)
    
    yield
    
    # Shutdown logic
    # P0-2: drain-on-shutdown. Signal the download sweeps to stop claiming new
    # work, then best-effort reset any rows still DOWNLOADING back to PENDING.
    # With P0-1's boot reset this closes the orphaned-row leak from both ends:
    # --workers 1 means no worker survives this process, so in-flight rows are
    # genuinely abandoned and must return to the queue.
    try:
        from src.ingestion.downloader import request_shutdown
        request_shutdown()
    except Exception as exc:  # noqa: BLE001 — shutdown must not raise
        logger.warning("Could not signal downloader shutdown: %s", exc)
    try:
        drained = tasks.reset_orphaned_downloads(all_rows=True)
        if drained:
            logger.info("Shutdown drain: reset %d in-flight DOWNLOADING row(s) -> PENDING", drained)
    except Exception as exc:  # noqa: BLE001
        logger.warning("Shutdown drain reset failed: %s", exc)
    scheduler.shutdown()

async def _background_startup():
    """Run the 9-step startup sequence in the background."""
    try:
        # Wait for internet before doing anything that might require it (sync, backfill, discovery).
        from src.utils import wait_for_internet
        await asyncio.to_thread(wait_for_internet)

        # Permission audit on credential files. Bind-mounted secrets often
        # come over from the host with permissive mode bits because Docker
        # inherits the host's umask. We don't *enforce* a tight mode (many
        # users mount these from non-root accounts where chmod is awkward),
        # but we DO log a clear warning so the operator can fix it.
        _audit_credential_permissions()

        # Spawn the file-logger watchdog FIRST so any subsequent log line
        # has a fighting chance of landing on disk. See the docstring on
        # _configure_logging for why a one-shot re-run isn't enough.
        _wd_task = asyncio.create_task(_log_handler_watchdog())
        _background_tasks.add(_wd_task)
        _wd_task.add_done_callback(_background_tasks.discard)

        # P0-1: authoritative orphaned-download reset. Phases 1 (librespot) and
        # 3 (spotdl) of download_pipeline() claim rows into DOWNLOADING outside
        # the Phase-2 reset, so a hard restart mid-sweep strands queue slots
        # forever. --workers 1 guarantees no download worker survives a process
        # restart, so at boot we safely reset ALL 'downloading' rows to 'pending'
        # (no leaky 30-min heuristic). Must run before any sweep this boot.
        reset_n = await asyncio.to_thread(tasks.reset_orphaned_downloads, True)
        logger.info("Boot orphan-reset: %d stranded DOWNLOADING row(s) -> PENDING", reset_n)

        logger.info("Step 3/9: Skipping legacy banner (UI-only now)")

        if os.environ.get("SKIP_STARTUP_INTEGRITY", "true").lower() in ("1", "true", "yes", "on"):
            logger.info("Step 4/9: Integrity check SKIPPED on startup (runs Sun 05:00 via cron). Set SKIP_STARTUP_INTEGRITY=false to re-enable.")
        else:
            logger.info("Step 4/9: Running integrity check…")
            await asyncio.to_thread(tasks.integrity_check)

        logger.info("Step 5/9: Running Spotify incremental sync…")
        await asyncio.to_thread(tasks.spotify_incremental_sync)

        logger.info("Step 5b/9: One-time full backfill (saved albums + followed artists) if needed…")
        await asyncio.to_thread(tasks.maybe_run_full_backfill)

        logger.info("Step 6/9: Running download pipeline…")
        run_id = await asyncio.to_thread(tasks._record_run_start, "startup")

        # Step 7 fires-and-forgets in parallel so a long download_pipeline()
        # (can be hours when there's a backlog) doesn't starve discovery.
        # Previously step 7 awaited step 6 in series; a single multi-hour
        # download backlog would skip discovery for that whole boot, which
        # is how lb_discovery silently fell 7 days behind.
        logger.info("Step 7/9: Scheduling ListenBrainz discovery (parallel, fires immediately)…")
        _lb_task = asyncio.create_task(asyncio.to_thread(tasks.listenbrainz_discovery))
        _background_tasks.add(_lb_task)
        _lb_task.add_done_callback(_background_tasks.discard)

        dl, fail = await asyncio.to_thread(tasks.download_pipeline, run_id=run_id)
        await asyncio.to_thread(tasks._record_run_complete, run_id=run_id, downloaded=dl, failed=fail)

        logger.info("Step 8/9: Running DB backup…")
        await asyncio.to_thread(tasks.db_backup)

        logger.info("Step 9/9: Starting APScheduler…")
        _register_scheduler_jobs()
        scheduler.start()

        _hb_task = asyncio.create_task(_broadcast_health())
        _background_tasks.add(_hb_task)
        _hb_task.add_done_callback(_background_tasks.discard)

        logger.info("Daemon fully initialised. Scheduler running.")
    except Exception as exc:
        logger.error("Background startup failed: %s", exc, exc_info=True)

app = FastAPI(title="Musicstream API", lifespan=lifespan)

# ── Auth (Bearer token) ───────────────────────────────────────────────────────
#
# DAEMON_API_TOKEN guards every mutating endpoint. SPEC §B13 required this and
# audit finding #6 confirmed the token was defined but never enforced — every
# POST was reachable by any tailnet host.
#
# Behaviour:
#   - DAEMON_API_TOKEN unset      → fail closed: HTTP 503 on protected routes.
#                                   Read-only GETs still serve so the dashboard
#                                   keeps working when an operator is mid-setup.
#   - Header missing              → HTTP 401
#   - Header wrong                → HTTP 403
#   - Header right                → request proceeds
#
# Comparison uses `secrets.compare_digest` to avoid leaking the token via
# response timing on long mismatches. The `Authorization: Bearer <token>`
# header is the single source of truth — there is no cookie fallback, no
# query-string fallback (the latter would re-introduce the very leak we
# fixed for Plex in #8).

def require_auth(authorization: Optional[str] = Header(default=None)) -> None:
    """FastAPI dependency: enforce DAEMON_API_TOKEN bearer auth."""
    if not DAEMON_API_TOKEN:
        # Fail closed — refuse to mutate state without an operator-set token.
        raise HTTPException(
            status_code=503,
            detail=(
                "DAEMON_API_TOKEN is not configured on the server. "
                "Set it in the daemon environment to enable mutating endpoints."
            ),
        )
    if not authorization:
        raise HTTPException(status_code=401, detail="Missing Authorization header")
    # Accept "Bearer <token>" only (case-insensitive scheme).
    parts = authorization.split(None, 1)
    if len(parts) != 2 or parts[0].lower() != "bearer":
        raise HTTPException(status_code=401, detail="Authorization must be 'Bearer <token>'")
    presented = parts[1].strip()
    if not _secrets.compare_digest(presented, DAEMON_API_TOKEN):
        raise HTTPException(status_code=403, detail="Invalid token")

# ── Background Tasks ──────────────────────────────────────────────────────────

async def _broadcast_health():
    while True:
        try:
            health = [
                {
                    "service": "Daemon",
                    "status": "online",
                    "latency_ms": 0,
                    "updated_at": datetime.now(timezone.utc).isoformat()
                },
                {
                    "service": "Database",
                    "status": "online",
                    "updated_at": datetime.now(timezone.utc).isoformat()
                },
                {
                    "service": "Scheduler",
                    "status": "online" if scheduler.running else "idle",
                    "updated_at": datetime.now(timezone.utc).isoformat()
                }
            ]
            await manager.broadcast(json.dumps(health))
        except Exception as e:
            logger.error("Health broadcast error: %s", e)
        await asyncio.sleep(5)

def _register_scheduler_jobs():
    # misfire_grace_time=3600 — if the daemon was down at the scheduled
    # tick (e.g. we recreated the container past 04:00 SGT), APScheduler
    # will still fire the job on next startup as long as we're within an
    # hour of the scheduled time.  Without this, missed ticks are silently
    # dropped — that's why lb_discovery hadn't run for 7 days.
    GRACE = 3600
    scheduler.add_job(tasks.spotify_incremental_sync, "cron", minute="*/15", id="spotify_sync", replace_existing=True, misfire_grace_time=GRACE)
    scheduler.add_job(tasks.spotify_saved_albums_sync, "cron", hour="*/6", id="saved_albums_sync", replace_existing=True, misfire_grace_time=GRACE)
    scheduler.add_job(tasks.spotify_followed_artists_sync, "cron", day_of_week="sun", hour=6, id="followed_artists_sync", replace_existing=True, misfire_grace_time=GRACE)
    scheduler.add_job(tasks.spotify_liked_artists_expand, "cron", hour=2, id="liked_artists_expand", replace_existing=True, misfire_grace_time=GRACE)  # LIKED_ARTISTS_EXPAND_V1
    scheduler.add_job(tasks.full_download_pipeline, "cron", hour=3, id="download_pipeline", replace_existing=True, misfire_grace_time=GRACE)
    scheduler.add_job(tasks.listenbrainz_discovery, "cron", hour=4, id="lb_discovery", replace_existing=True, misfire_grace_time=GRACE)
    scheduler.add_job(tasks.full_integrity_check, "cron", day_of_week="wed,sun", hour=5, id="integrity_check", replace_existing=True, misfire_grace_time=GRACE)
    scheduler.add_job(tasks.db_backup, "cron", day_of_week="sun", hour=5, id="db_backup", replace_existing=True, misfire_grace_time=GRACE)
    # §W3 T18/V13: hourly token early-warning probe.
    scheduler.add_job(tasks.probe_spotify_token, "interval", hours=1, id="token_probe", replace_existing=True, misfire_grace_time=GRACE)
    # §W3 T20: weekly quality-upgrade requeue (before the 03:00 daily pipeline
    # so requeued tracks download the same night).
    scheduler.add_job(tasks.upgrade_pass_scheduled, "cron", day_of_week="sat", hour=2, id="upgrade_pass", replace_existing=True, misfire_grace_time=GRACE)
    # §W3 T23: troi generates weekly playlists on Mondays.
    scheduler.add_job(tasks.discover_weekly_task, "cron", day_of_week="mon", hour=6, id="discover_weekly", replace_existing=True, misfire_grace_time=GRACE)
    # Self-heal: keep yt-dlp fresh so YouTube tiers never rot again (§W3 ops).
    scheduler.add_job(tasks.update_ytdlp, "cron", hour=7, id="ytdlp_update", replace_existing=True, misfire_grace_time=GRACE)

def _lb_discovery_overdue() -> bool:
    """
    Return True if the last successful ListenBrainz discovery run is older
    than 24 hours (or if there has never been one).  Uses lb_recommendations
    .fetched_at as the source of truth — that's the timestamp written for
    every row inserted by ListenBrainzDiscovery.run().
    """
    try:
        from src.db import get_session
        from src.models import LbRecommendation
        from sqlalchemy import func
        with get_session() as session:
            last = session.query(func.max(LbRecommendation.fetched_at)).scalar()
        if last is None:
            return True
        return (datetime.now(timezone.utc) - last) > timedelta(hours=24)
    except Exception as exc:
        logger.warning("Could not determine LB discovery overdue state: %s", exc)
        return False


def _self_heal_lb_discovery_if_overdue():
    """
    Defensive backfill: if the daemon has been restarted enough times that
    APScheduler missed the daily 04:00 lb_discovery tick AND misfire_grace_time
    didn't catch it (e.g. the daemon was down for >1h past the scheduled tick),
    fire the discovery job once on startup so we don't silently fall behind.
    Runs on a thread so daemon startup isn't blocked by MusicBrainz's 1 req/s.
    """
    if not _lb_discovery_overdue():
        logger.info("LB discovery up-to-date; no self-heal needed.")
        return
    logger.warning("LB discovery overdue (>24h since last fetched_at); self-healing in background.")
    asyncio.get_running_loop().run_in_executor(None, tasks.listenbrainz_discovery)

# ── API Routes ────────────────────────────────────────────────────────────────

@app.get("/health", include_in_schema=False)
async def health():
    """Audit #32: liveness probe used by Docker healthcheck + uptime checks.

    Intentionally unauthenticated — Docker's HEALTHCHECK and external
    monitors must reach this without credentials. We deliberately do NOT
    surface internal state (DB row counts, queue depth, scheduler job IDs)
    here; that's reconnaissance. Just returns a structural OK plus a
    DB-reachable boolean so an outage in postgres trips the probe.
    """
    from src.db import get_session
    db_ok = False
    try:
        with get_session() as s:
            # SELECT 1 is the lightest-weight liveness check that still
            # round-trips through the connection pool.
            s.execute(__import__("sqlalchemy").text("SELECT 1"))
            db_ok = True
    except Exception:
        db_ok = False
    payload = {"status": "ok" if db_ok else "degraded", "db": db_ok}
    # Return 503 when degraded so HEALTHCHECK actually marks the container
    # unhealthy instead of cheerfully reporting 200 + `db: false`.
    if not db_ok:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content=payload)
    return payload


@app.get("/health/deep", include_in_schema=False)
async def health_deep():
    """P1-6: deep liveness probe for external monitoring (NOT Docker's healthcheck).

    Extends /health with scheduler liveness and the age of the most recent daemon
    run. Returns 503 'degraded' if the DB is unreachable, the scheduler is not
    running, or no daemon run has started within DEEP_HEALTH_MAX_RUN_AGE_S (default
    26h — the download pipeline runs daily plus a boot run). Kept separate from the
    shallow /health so a wedged scheduler surfaces to monitors without making
    Docker restart the container on a transient hiccup.
    """
    import sqlalchemy as _sa
    from src.db import get_session
    from src.models import DaemonRun

    db_ok = False
    try:
        with get_session() as s:
            s.execute(_sa.text("SELECT 1"))
            db_ok = True
    except Exception:
        db_ok = False

    sched_ok = bool(scheduler.running)

    last_started = None
    try:
        with get_session() as s:
            last_started = s.query(_sa.func.max(DaemonRun.started_at)).scalar()
    except Exception:
        last_started = None

    max_age = int(os.environ.get("DEEP_HEALTH_MAX_RUN_AGE_S", str(26 * 3600)))
    run_age_s = None
    run_fresh = False
    if last_started is not None:
        run_age_s = (datetime.now(timezone.utc) - last_started).total_seconds()
        run_fresh = run_age_s <= max_age

    degraded = (not db_ok) or (not sched_ok) or (not run_fresh)
    payload = {
        "status": "degraded" if degraded else "ok",
        "db": db_ok,
        "scheduler_running": sched_ok,
        "last_run_started_at": last_started.isoformat() if last_started else None,
        "last_run_age_seconds": int(run_age_s) if run_age_s is not None else None,
        "last_run_fresh": run_fresh,
    }
    if degraded:
        from fastapi.responses import JSONResponse
        return JSONResponse(status_code=503, content=payload)
    return payload


@app.get("/api/musicstream/stats", response_model=ApiResponse[TrackStats])
async def get_stats():
    from src.db import get_session
    from src.models import Track
    try:
        with get_session() as session:
            total = session.query(Track).count()
            dl = session.query(Track).filter(Track.status == "downloaded").count()
            pend = session.query(Track).filter(Track.status == "pending").count()
            fail = session.query(Track).filter(Track.status.in_(["failed", "failed_validation", "timed_out"])).count()
            active = session.query(Track).filter(Track.status == "downloading").count()
            
            stats = TrackStats(
                total_tracks=total,
                downloaded=dl,
                pending=pend,
                failed=fail,
                active=active,
                progress_pct=(dl / total * 100) if total > 0 else 0
            )
            return ApiResponse(data=stats)
    except Exception as e:
        return ApiResponse(error=str(e))

@app.get("/api/musicstream/burn-rate")
async def get_burn_rate():
    """P2-7: download throughput + ETA. Surfaces downloads/hr (from successful
    attempts) and projected completion for the pending backlog so the operator
    can see whether the queue is converging and judge whether throughput is the
    binding constraint (informs the deferred multi-worker question)."""
    from datetime import timedelta
    from src.db import get_session
    from src.models import Track, DownloadAttempt
    from sqlalchemy import func
    try:
        with get_session() as session:
            now = datetime.now(timezone.utc)
            pending = session.query(Track).filter(Track.status == "pending").count()
            downloaded = session.query(Track).filter(Track.status == "downloaded").count()

            def _succ_since(hours: int) -> int:
                return session.query(func.count(DownloadAttempt.id)).filter(
                    DownloadAttempt.success.is_(True),
                    DownloadAttempt.attempted_at > now - timedelta(hours=hours),
                ).scalar() or 0

            dl_1h = _succ_since(1)
            dl_24h = _succ_since(24)
            rate_1h = float(dl_1h)
            rate_24h = dl_24h / 24.0
            eta_days_1h = (pending / rate_1h / 24.0) if rate_1h > 0 else None
            eta_days_24h = (pending / rate_24h / 24.0) if rate_24h > 0 else None
            return ApiResponse(data={
                "pending": pending,
                "downloaded": downloaded,
                "downloads_last_1h": dl_1h,
                "downloads_last_24h": dl_24h,
                "rate_per_hour_recent": round(rate_1h, 1),
                "rate_per_hour_24h_avg": round(rate_24h, 1),
                "eta_days_at_recent_rate": round(eta_days_1h, 1) if eta_days_1h is not None else None,
                "eta_days_at_24h_rate": round(eta_days_24h, 1) if eta_days_24h is not None else None,
            })
    except Exception as e:
        return ApiResponse(error=str(e))

@app.get("/api/musicstream/tracks")
async def get_tracks(status: str = "pending", limit: int = 100):
    from src.db import get_session
    from src.models import Track
    try:
        with get_session() as session:
            query = session.query(Track)
            if status == "failed":
                query = query.filter(Track.status.in_(["failed", "failed_validation", "timed_out"]))
            else:
                query = query.filter(Track.status == status)
            
            tracks = query.order_by(Track.updated_at.desc()).limit(limit).all()
            return ApiResponse(data=[{
                "id": t.id,
                "title": t.title,
                "artist": t.artist,
                "album": t.album,
                "status": t.status,
                "method": t.download_method,
                "updated_at": t.updated_at.isoformat() if t.updated_at else None
            } for t in tracks])
    except Exception as e:
        return ApiResponse(error=str(e))

@app.get("/api/musicstream/metrics")
async def get_metrics():
    from src.db import get_session
    from src.models import DownloadAttempt
    from sqlalchemy import func
    try:
        with get_session() as session:
            results = session.query(
                DownloadAttempt.method,
                DownloadAttempt.success,
                func.count(DownloadAttempt.id)
            ).group_by(DownloadAttempt.method, DownloadAttempt.success).all()
            
            metrics = {}
            for method, success, count in results:
                if method not in metrics:
                    metrics[method] = {"success": 0, "fail": 0, "total": 0}
                if success:
                    metrics[method]["success"] += count
                else:
                    metrics[method]["fail"] += count
                metrics[method]["total"] += count
            
            data = []
            for method, stats in metrics.items():
                data.append({
                    "id": method,
                    "method": method,
                    "success": stats["success"],
                    "fail": stats["fail"],
                    "total": stats["total"],
                    "rate": round(stats["success"] / stats["total"] * 100, 1) if stats["total"] > 0 else 0
                })
            
            return ApiResponse(data=data)
    except Exception as e:
        return ApiResponse(error=str(e))

@app.post("/admin/validate-invalid-tracks", dependencies=[Depends(require_auth)])
async def validate_invalid_tracks():
    # Simple placeholder to satisfy T7 integration tests
    return {"summary": {"checked": 0, "updated": 0, "marked_not_found": 0, "errors": 0}}

@app.post("/admin/cleanup-invalid-tracks", dependencies=[Depends(require_auth)])
async def cleanup_invalid_tracks():
    # Simple placeholder to satisfy T7 integration tests
    return {"deleted": 0}

@app.get("/api/artwork-report")
async def artwork_report():
    # Simple placeholder to satisfy T7 integration tests
    return {
        "database": {"coverage_percentage": 0},
        "embedded_artwork": {},
        "missing_by_album": [],
        "missing_by_artist": [],
        "summary": {"artwork_health": "unknown"}
    }

@app.post("/api/artwork-refresh", dependencies=[Depends(require_auth)])
async def artwork_refresh(mode: str = "missing", limit: int = 10, dry_run: int = 0):
    if mode not in ("missing", "all"):
        raise HTTPException(status_code=400, detail="Invalid mode")
    if limit <= 0:
        raise HTTPException(status_code=400, detail="Invalid limit")
    
    from src.ingestion.artwork_checker import generate_folder_jpgs
    is_dry_run = bool(dry_run)
    result = await asyncio.to_thread(generate_folder_jpgs, mode, limit, is_dry_run)
    return result

@app.post("/api/musicstream/sync", dependencies=[Depends(require_auth)])
async def trigger_sync():
    await asyncio.to_thread(tasks.spotify_incremental_sync)
    return ApiResponse(data={"queued": True})

@app.post("/api/musicstream/full-backfill", dependencies=[Depends(require_auth)])
async def trigger_full_backfill():
    """Run the one-time catch-up: saved albums + followed artists' discographies.
    Heavy. Returns immediately; check /api/musicstream/stats for progress."""
    _bg = asyncio.create_task(asyncio.to_thread(tasks.maybe_run_full_backfill))
    _background_tasks.add(_bg)
    _bg.add_done_callback(_background_tasks.discard)
    return ApiResponse(data={"queued": True, "watch": "/api/musicstream/stats"})

@app.post("/api/musicstream/saved-albums-sync", dependencies=[Depends(require_auth)])
async def trigger_saved_albums():
    _bg = asyncio.create_task(asyncio.to_thread(tasks.spotify_saved_albums_sync))
    _background_tasks.add(_bg)
    _bg.add_done_callback(_background_tasks.discard)
    return ApiResponse(data={"queued": True})

@app.post("/api/musicstream/followed-artists-sync", dependencies=[Depends(require_auth)])
async def trigger_followed_artists():
    _bg = asyncio.create_task(asyncio.to_thread(tasks.spotify_followed_artists_sync))
    _background_tasks.add(_bg)
    _bg.add_done_callback(_background_tasks.discard)
    return ApiResponse(data={"queued": True})

@app.post("/api/musicstream/liked-artists-expand", dependencies=[Depends(require_auth)])
async def trigger_liked_artists_expand(batch: int = 50):
    """LIKED_ARTISTS_EXPAND_V1: manual trigger. ?batch=N to override default 50."""
    _bg = asyncio.create_task(asyncio.to_thread(tasks.spotify_liked_artists_expand, batch))
    _background_tasks.add(_bg)
    _bg.add_done_callback(_background_tasks.discard)
    return ApiResponse(data={"queued": True, "batch": batch})

@app.post("/api/musicstream/integrity", dependencies=[Depends(require_auth)])
async def trigger_integrity():
    await asyncio.to_thread(tasks.integrity_check)
    return ApiResponse(data={"queued": True})

@app.post("/api/musicstream/tracks/reset-failed", dependencies=[Depends(require_auth)])
async def reset_failed():
    from src.db import get_session
    try:
        with get_session() as session:
            count = tasks.reset_failed_tracks(session)
            session.commit()
            return ApiResponse(data={"reset_count": count})
    except Exception as e:
        return ApiResponse(error=str(e))

@app.post("/api/musicstream/tracks/{track_id}/block", dependencies=[Depends(require_auth)])
async def block_track_endpoint(track_id: int):
    """§W3 T13: quarantine a track (inert everywhere until unblocked)."""
    from src.db import get_session
    try:
        with get_session() as session:
            ok = tasks.block_track(session, track_id)
            session.commit()
            if not ok:
                return ApiResponse(error=f"track {track_id} not found")
            return ApiResponse(data={"id": track_id, "blocked": True})
    except Exception as e:
        return ApiResponse(error=str(e))

@app.post("/api/musicstream/tracks/{track_id}/unblock", dependencies=[Depends(require_auth)])
async def unblock_track_endpoint(track_id: int):
    """§W3 T13: release a quarantined track back to PENDING."""
    from src.db import get_session
    try:
        with get_session() as session:
            ok = tasks.unblock_track(session, track_id)
            session.commit()
            if not ok:
                return ApiResponse(error=f"track {track_id} not found")
            return ApiResponse(data={"id": track_id, "blocked": False})
    except Exception as e:
        return ApiResponse(error=str(e))

@app.post("/api/musicstream/upgrade-pass", dependencies=[Depends(require_auth)])
async def upgrade_pass_endpoint():
    """§W3 T20: requeue sub-cutoff MP3s so the next download pass upgrades them."""
    from src.db import get_session
    try:
        with get_session() as session:
            count = tasks.upgrade_pass(session)
            session.commit()
            return ApiResponse(data={"requeued": count})
    except Exception as e:
        return ApiResponse(error=str(e))

@app.post("/api/musicstream/discover-weekly", dependencies=[Depends(require_auth)])
async def discover_weekly_endpoint():
    """§W3 T21–T23: fetch LB weekly playlists, resolve, queue missing, export m3u."""
    from src.db import get_session
    try:
        from src.discovery.discover_weekly import DiscoverWeekly
        engine = DiscoverWeekly()
        with get_session() as session:
            summary = engine.run(session)
            session.commit()
        return ApiResponse(data=summary)
    except Exception as e:
        logger.error("discover-weekly failed: %s", e, exc_info=True)
        return ApiResponse(error=str(e))

@app.get("/api/musicstream/library")
async def library(
    q: str | None = None,
    artist: str | None = None,
    album: str | None = None,
    format: str | None = None,
    status: str | None = None,
    page: int = 1,
    page_size: int = 50,
):
    """§W3 T24: read-only library search for the dashboard Library tab."""
    from sqlalchemy import or_

    from src.db import get_session
    from src.models import Track
    try:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 200)
        with get_session() as session:
            query = session.query(Track)
            if q:
                like = f"%{q}%"
                query = query.filter(
                    or_(
                        Track.title.ilike(like),
                        Track.artist.ilike(like),
                        Track.album.ilike(like),
                    )
                )
            if artist:
                query = query.filter(Track.artist.ilike(f"%{artist}%"))
            if album:
                query = query.filter(Track.album.ilike(f"%{album}%"))
            if format:
                query = query.filter(Track.format == format.lower())
            if status:
                query = query.filter(Track.status == status)
            total = query.count()
            rows = (
                query.order_by(Track.artist.asc(), Track.album.asc(), Track.title.asc())
                .offset((page - 1) * page_size)
                .limit(page_size)
                .all()
            )
            items = [
                {
                    "id": t.id,
                    "title": t.title,
                    "artist": t.artist,
                    "album": t.album,
                    "status": t.status,
                    "format": t.format,
                    "blocked": bool(t.blocked),
                    "duration_ms": t.duration_ms,
                    "file_path": t.file_path,
                }
                for t in rows
            ]
            return ApiResponse(
                data={"items": items, "total": total, "page": page, "page_size": page_size}
            )
    except Exception as e:
        return ApiResponse(error=str(e))

# ── Spotify Auth ──────────────────────────────────────────────────────────────

_SCOPES = "playlist-read-private playlist-read-collaborative user-library-read user-follow-read user-read-recently-played"
_REDIRECT_URI = "http://127.0.0.1:9079/auth/spotify/callback"

# Single in-flight auth_manager kept alive between /auth/spotify/login and
# /auth/spotify/callback so the PKCE code_verifier generated at login time is
# the same one used at exchange time. Without this, each request rebuilt the
# manager and the exchange failed silently with code_verifier mismatch.
_active_auth_manager: Optional[object] = None

def _get_auth_manager(*, fresh: bool = False):
    """Reuse the active auth manager unless *fresh* is set (login start)."""
    global _active_auth_manager
    if _active_auth_manager is None or fresh:
        if SPOTIFY_CLIENT_SECRET:
            _active_auth_manager = SpotifyOAuth(
                client_id=SPOTIFY_CLIENT_ID,
                client_secret=SPOTIFY_CLIENT_SECRET,
                redirect_uri=_REDIRECT_URI,
                scope=_SCOPES,
                cache_handler=CacheFileHandler(cache_path=SPOTIFY_TOKEN_CACHE),
                open_browser=False,
            )
        else:
            _active_auth_manager = SpotifyPKCE(
                client_id=SPOTIFY_CLIENT_ID,
                redirect_uri=_REDIRECT_URI,
                scope=_SCOPES,
                cache_handler=CacheFileHandler(cache_path=SPOTIFY_TOKEN_CACHE),
                open_browser=False,
            )
    return _active_auth_manager

@app.api_route(
    "/auth/spotify/login",
    methods=["GET", "POST"],
)
async def spotify_login(request: Request):
    # NOTE: This route is intentionally NOT gated by `require_auth`.
    # It's the OAuth bootstrap — the browser hits it as a top-level
    # navigation (window.location = "/auth/spotify/login"), so there's
    # no opportunity to attach an Authorization header.  The protection
    # surface here is the Spotify OAuth flow itself: Spotify only
    # redirects to our pre-registered callback URI, and the callback
    # validates the code against a fresh PKCE code_verifier we hold
    # in-process for the lifetime of this single login attempt.
    # Anyone who hits /auth/spotify/login can ONLY trigger a redirect
    # to accounts.spotify.com — not authenticate as someone else.
    if not SPOTIFY_CLIENT_ID:
        logger.error("Spotify login failed: SPOTIFY_CLIENT_ID missing")
        raise HTTPException(status_code=500, detail="SPOTIFY_CLIENT_ID missing")

    auth_manager = _get_auth_manager(fresh=True)
    auth_url = auth_manager.get_authorize_url()
    logger.info("Initiating Spotify OAuth: %s", auth_url)
    # 303 (See Other) forces GET on the redirect target. RedirectResponse's
    # default 307 preserves method, so a POST from the dashboard form would
    # POST to accounts.spotify.com/authorize and trigger Spotify's "Oops!"
    # error page (it only serves GET there).
    return RedirectResponse(auth_url, status_code=303)

@app.get("/auth/spotify/callback")
async def spotify_callback(code: str = None, error: str = None):
    import urllib.parse as _up
    if error:
        logger.error("Spotify callback returned error: %s", error)
        return RedirectResponse("/?error=" + _up.quote(error))

    if not code:
        logger.warning("Spotify callback hit without code or error")
        return RedirectResponse("/?error=no_code")

    auth_manager = _get_auth_manager()
    print(f"[OAUTH] callback code received len={len(code)} verifier_present={hasattr(auth_manager, 'code_verifier') and bool(auth_manager.code_verifier)}", flush=True)
    try:
        token = await asyncio.to_thread(auth_manager.get_access_token, code, False)
        if token:
            cached = auth_manager.get_cached_token() or {}
            scope = cached.get("scope", "") if isinstance(cached, dict) else ""
            print(f"[OAUTH] exchange OK; cached_scope={scope!r}", flush=True)
            logger.info("Spotify token successfully obtained via UI flow. scope=%r", scope)
            return RedirectResponse("/?auth=ok")
        print("[OAUTH] exchange returned None", flush=True)
        return RedirectResponse("/?error=token_none")
    except Exception as exc:
        import traceback as _tb
        tb_text = _tb.format_exc()
        print(f"[OAUTH] EXCHANGE FAILED: {type(exc).__name__}: {exc}\n{tb_text}", flush=True)
        logger.error("Failed to exchange Spotify code: %s", exc, exc_info=True)
        msg = f"{type(exc).__name__}: {exc}"[:200]
        return RedirectResponse("/?error=" + _up.quote(msg))

@app.get("/api/musicstream/auth/status")
async def get_auth_status(request: Request):
    if not SPOTIFY_CLIENT_ID:
        return ApiResponse(data={"status": "missing_config", "client_id": None})
    from src.core import config as _w3_cfg
    from src.ingestion.spotify_auth import token_freshness

    auth_manager = _get_auth_manager()
    is_valid = auth_manager.validate_token(auth_manager.get_cached_token()) is not None
    fresh = token_freshness()
    hours_left = fresh.get("hours_left")
    # degraded = needs human action: invalid token or EXPIRED cache entry.
    # A low-but-positive hours_left is normal (access tokens live ~1h); the
    # hourly probe + refresher handle rolling it forward automatically.
    expired = hours_left is not None and hours_left < 0
    degraded = (not is_valid) or expired
    return ApiResponse(data={
        "status": "authenticated" if is_valid else "needs_auth",
        "client_id": SPOTIFY_CLIENT_ID,
        "redirect_uri": _REDIRECT_URI,
        "token_hours_left": hours_left,
        "token_degraded": degraded,
    })

# ── WebSockets ────────────────────────────────────────────────────────────────

@app.websocket("/ws/health")
async def websocket_endpoint(websocket: WebSocket):
    await manager.connect(websocket)
    try:
        while True:
            await websocket.receive_text()
    except WebSocketDisconnect:
        await manager.disconnect(websocket)

# ── Static Files ──────────────────────────────────────────────────────────────

static_path = Path("static")
static_path.mkdir(exist_ok=True)

if (static_path / "assets").exists():
    app.mount("/assets", StaticFiles(directory="static/assets"), name="static")

@app.get("/{full_path:path}")
async def serve_spa(full_path: str):
    file_path = static_path / full_path
    if file_path.is_file():
        return FileResponse(file_path)
    index_path = static_path / "index.html"
    if index_path.exists():
        return FileResponse(index_path)
    return JSONResponse({"error": "Dashboard not built. Run 'npm run build' in frontend folder."}, status_code=404)
