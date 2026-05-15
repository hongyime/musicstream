"""
musicstream/daemon.py — APScheduler + Flask control plane

Orchestrates the full musicstream pipeline:
  - Startup sequence: DB connect, migrations, integrity check, Spotify sync,
    download pipeline, ListenBrainz discovery, DB backup, scheduler start
  - APScheduler cron jobs (Asia/Singapore timezone)
  - Flask HTTP control plane on port 9079
  - 3 rotating log handlers (musicstream.log, errors.log, daemon.log)
"""

from __future__ import annotations

import logging
import logging.handlers
import os
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from flask import Flask, jsonify, request as flask_request

# ── Module-level singletons ───────────────────────────────────────────────────

app = Flask(__name__)
scheduler = BackgroundScheduler(timezone="Asia/Singapore")
_start_time = time.time()

# ── Logging setup ─────────────────────────────────────────────────────────────

_LOG_DIR = Path("logs")
_BACKUP_DIR = Path("backups")
_MAX_BYTES = 5 * 1024 * 1024   # 5 MB
_BACKUP_COUNT = 3
_MAX_BACKUPS = 14


def _configure_logging() -> None:
    """
    Configure 3 rotating log handlers:
      - logs/musicstream.log  — general INFO+
      - logs/errors.log       — errors only (WARNING+)
      - logs/daemon.log       — startup + run reports (INFO+)
    Each handler uses RotatingFileHandler(5MB, 3 backups).
    """
    _LOG_DIR.mkdir(parents=True, exist_ok=True)

    fmt = logging.Formatter(
        "%(asctime)s %(levelname)-8s %(name)s — %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # ── Root logger ────────────────────────────────────────────────────────────
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)

    # Console handler (INFO+)
    console_handler = logging.StreamHandler()
    console_handler.setLevel(logging.INFO)
    console_handler.setFormatter(fmt)
    root.addHandler(console_handler)

    # ── musicstream.log — general INFO+ ───────────────────────────────────────
    ms_handler = logging.handlers.RotatingFileHandler(
        _LOG_DIR / "musicstream.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    ms_handler.setLevel(logging.INFO)
    ms_handler.setFormatter(fmt)
    root.addHandler(ms_handler)

    # ── errors.log — WARNING+ (failed tracks, structured errors) ──────────────
    err_handler = logging.handlers.RotatingFileHandler(
        _LOG_DIR / "errors.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    err_handler.setLevel(logging.WARNING)
    err_handler.setFormatter(fmt)

    # Attach to the musicstream.errors logger used by all modules
    errors_logger = logging.getLogger("musicstream.errors")
    errors_logger.setLevel(logging.WARNING)
    errors_logger.addHandler(err_handler)
    errors_logger.propagate = False  # don't double-log to root

    # Also attach to the bare "errors" logger used by downloader.py
    bare_errors_logger = logging.getLogger("errors")
    bare_errors_logger.setLevel(logging.WARNING)
    bare_errors_logger.addHandler(err_handler)
    bare_errors_logger.propagate = False

    # ── daemon.log — startup + run reports (INFO+) ────────────────────────────
    daemon_handler = logging.handlers.RotatingFileHandler(
        _LOG_DIR / "daemon.log",
        maxBytes=_MAX_BYTES,
        backupCount=_BACKUP_COUNT,
        encoding="utf-8",
    )
    daemon_handler.setLevel(logging.INFO)
    daemon_handler.setFormatter(fmt)

    daemon_logger = logging.getLogger("musicstream.daemon")
    daemon_logger.setLevel(logging.INFO)
    daemon_logger.addHandler(daemon_handler)
    daemon_logger.propagate = True  # also goes to root → musicstream.log


_configure_logging()
logger = logging.getLogger("musicstream.daemon")


# ── Pipeline helpers ──────────────────────────────────────────────────────────

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
    path = _LOG_DIR / "errors.log"
    try:
        return path.stat().st_size / (1024 * 1024)
    except OSError:
        return 0.0


# ── Startup banner ────────────────────────────────────────────────────────────

def _print_startup_banner() -> None:
    """
    Print the startup banner using rich Panel (PRD §13.3 format).

    ╔════════════════════════════════════════════════════╗
    ║         MUSICSTREAM DAEMON v3.0                    ║
    ╠════════════════════════════════════════════════════╣
    ║ Last full run:   2026-04-22 03:00 SGT              ║
    ║ Downloaded:  44  │  Failed:    1  │  Requeued:  0  ║
    ║ DB tracks:  4219  │  Missing:   0  │  Corrupt:  0  ║
    ║ LB recs:    200   │  Ingested: 12                  ║
    ║ errors.log: 1.2MB / 5MB                            ║
    ╚════════════════════════════════════════════════════╝
    """
    from rich.console import Console
    from rich.panel import Panel

    rich_console = Console()

    # Gather stats
    last_run = _get_last_daemon_run()
    db_tracks = _get_db_track_count()
    errors_mb = _get_errors_log_size()

    if last_run and last_run.started_at:
        # Format in SGT (UTC+8)
        try:
            import zoneinfo
            sgt = zoneinfo.ZoneInfo("Asia/Singapore")
            run_dt = last_run.started_at.astimezone(sgt)
            last_run_str = run_dt.strftime("%Y-%m-%d %H:%M SGT")
        except Exception:
            last_run_str = last_run.started_at.strftime("%Y-%m-%d %H:%M UTC")
        downloaded = last_run.tracks_downloaded
        failed = last_run.tracks_failed
        requeued = last_run.tracks_requeued
    else:
        last_run_str = "never"
        downloaded = 0
        failed = 0
        requeued = 0

    # LB recommendation stats
    lb_total = 0
    lb_ingested = 0
    try:
        from src.db import get_session
        from src.models import LbRecommendation
        with get_session() as session:
            lb_total = session.query(LbRecommendation).count()
            lb_ingested = (
                session.query(LbRecommendation)
                .filter(LbRecommendation.status == "ingested")
                .count()
            )
    except Exception:
        pass

    # Integrity stats from last run notes (best-effort)
    missing = 0
    corrupt = 0

    lines = [
        "[bold cyan]MUSICSTREAM DAEMON v3.0[/bold cyan]",
        "",
        f"Last full run:   {last_run_str}",
        f"Downloaded: {downloaded:>3}  │  Failed: {failed:>4}  │  Requeued: {requeued:>2}",
        f"DB tracks: {db_tracks:>5}  │  Missing: {missing:>3}  │  Corrupt: {corrupt:>2}",
        f"LB recs:   {lb_total:>5}  │  Ingested: {lb_ingested:>2}",
        f"errors.log: {errors_mb:.1f}MB / 5MB",
    ]
    
    # Add staging mode warning
    if os.environ.get("DISABLE_DOWNLOADS", "").lower() in ("1", "true", "yes", "on"):
        lines.append("")
        lines.append("[bold red]DOWNLOADS DISABLED (STAGING MODE)[/bold red]")

    panel = Panel(
        "\n".join(lines),
        border_style="cyan",
        padding=(0, 2),
    )
    rich_console.print(panel)
    logger.info(
        "Startup banner: last_run=%s db_tracks=%d downloaded=%d failed=%d",
        last_run_str, db_tracks, downloaded, failed,
    )


# ── Pipeline functions ────────────────────────────────────────────────────────

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


def spotify_incremental_sync() -> None:
    """Run Spotify incremental sync and log new track count."""
    logger.info("Running Spotify incremental sync…")
    try:
        from src.db import get_session
        from src.ingestion.scraper import SpotifyScraper
        client_id = os.environ.get("SPOTIFY_CLIENT_ID", "")
        scraper = SpotifyScraper(client_id=client_id)
        with get_session() as session:
            new_tracks = scraper.incremental_sync(session)
        logger.info("Spotify incremental sync complete: %d new tracks", new_tracks)
    except Exception as exc:
        logger.error("Spotify incremental sync failed: %s", exc, exc_info=True)


def download_pipeline() -> tuple[int, int]:
    """Run the download pipeline for all pending tracks. Returns (downloaded, failed)."""
    logger.info("Running download pipeline…")
    try:
        from src.db import get_session
        from src.ingestion.downloader import DownloadOrchestrator
        orchestrator = DownloadOrchestrator()

        # Phase 1: librespot — every track gets a genuine Spotify CDN attempt first.
        # Single worker, no semaphore, bounded at 2h so phases 2+3 still run.
        try:
            with get_session() as session:
                lib_dl, lib_fail = orchestrator.download_pending_librespot(session)
            logger.info("librespot sweep: downloaded=%d failed=%d", lib_dl, lib_fail)
        except Exception as exc:
            logger.error("librespot sweep failed (non-fatal): %s", exc, exc_info=True)
            lib_dl = 0

        # Phase 2: 12-worker batch — tier2 → tier4 → tier5 for remaining tracks.
        with get_session() as session:
            downloaded, failed = orchestrator.download_pending(session)
        logger.info(
            "Download pipeline complete: downloaded=%d failed=%d",
            downloaded, failed,
        )
        downloaded += lib_dl

        # Phase 3: spotdl — single worker, top 100 still-pending tracks.
        try:
            with get_session() as session:
                sdl_dl, sdl_fail = orchestrator.download_pending_spotdl(session)
            logger.info("spotdl sweep: downloaded=%d failed=%d", sdl_dl, sdl_fail)
            downloaded += sdl_dl
        except Exception as exc:
            logger.error("spotdl sweep failed (non-fatal): %s", exc, exc_info=True)
        return downloaded, failed
    except Exception as exc:
        logger.error("Download pipeline failed: %s", exc, exc_info=True)
        return 0, 0


def listenbrainz_discovery() -> None:
    """Run ListenBrainz discovery and Plex playlist sync."""
    logger.info("Running ListenBrainz discovery…")
    try:
        from src.db import get_session
        from src.discovery.listenbrainz import ListenBrainzDiscovery
        from src.discovery.plex_playlists import PlexPlaylistSync
        discovery = ListenBrainzDiscovery()
        with get_session() as session:
            new_tracks = discovery.run(session)
        logger.info("ListenBrainz discovery complete: %d new tracks", new_tracks)

        # Sync Plex playlist for current month
        now = datetime.now(timezone.utc)
        month_name = now.strftime("%B")
        year = now.year
        plex_sync = PlexPlaylistSync()
        with get_session() as session:
            plex_sync.sync_discovery_playlist(session, month=month_name, year=year)
    except Exception as exc:
        logger.error("ListenBrainz discovery failed: %s", exc, exc_info=True)


def db_backup() -> Optional[str]:
    """
    Run pg_dump to backups/musicstream_{YYYYMMDD_HHMMSS}.sql.
    Prune to keep only the 14 most recent backups.
    Returns the backup file path on success, None on failure.
    """
    _BACKUP_DIR.mkdir(parents=True, exist_ok=True)

    timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    backup_filename = f"musicstream_{timestamp}.sql"
    backup_path = _BACKUP_DIR / backup_filename

    database_url = os.environ.get("DATABASE_URL", "")
    if not database_url:
        logger.error("DATABASE_URL not set; cannot run pg_dump")
        return None

    # Parse DATABASE_URL to avoid passing password on the CLI (visible in ps/logs).
    import urllib.parse as _urlparse
    _u = _urlparse.urlparse(database_url)
    _pg_env = {
        **os.environ,
        "PGPASSWORD": _u.password or "",
    }
    _pg_cmd = [
        "pg_dump",
        "-h", _u.hostname or "localhost",
        "-p", str(_u.port or 5432),
        "-U", _u.username or "musicstream",
        "-d", _u.path.lstrip("/"),
        "--no-password",
        "--file", str(backup_path),
    ]

    logger.info("Running pg_dump → %s", backup_path)
    try:
        result = subprocess.run(
            _pg_cmd,
            env=_pg_env,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if result.returncode != 0:
            logger.error(
                "pg_dump failed (exit %d): %s",
                result.returncode,
                result.stderr[:500],
            )
            # Remove empty/partial file
            if backup_path.exists():
                backup_path.unlink()
            return None
    except FileNotFoundError:
        logger.error("pg_dump not found; ensure postgresql-client is installed")
        return None
    except subprocess.TimeoutExpired:
        logger.error("pg_dump timed out after 300s")
        if backup_path.exists():
            backup_path.unlink()
        return None
    except Exception as exc:
        logger.error("pg_dump error: %s", exc, exc_info=True)
        return None

    size_bytes = backup_path.stat().st_size
    logger.info("pg_dump complete: %s (%d bytes)", backup_path, size_bytes)

    # Prune to keep only the 14 most recent backups
    _prune_backups()

    return str(backup_path)


def _prune_backups() -> None:
    """Keep only the 14 most recent .sql files in the backups/ directory."""
    try:
        sql_files = sorted(
            _BACKUP_DIR.glob("musicstream_*.sql"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
        for old_file in sql_files[_MAX_BACKUPS:]:
            old_file.unlink()
            logger.info("Pruned old backup: %s", old_file)
    except Exception as exc:
        logger.warning("Backup pruning failed: %s", exc)


# ── Full pipeline wrappers (used by scheduler + HTTP endpoints) ───────────────

def full_download_pipeline() -> None:
    """Full download pipeline wrapper for scheduler."""
    # Check if downloads are disabled (staging/development mode)
    if os.environ.get("DISABLE_DOWNLOADS", "").lower() in ("1", "true", "yes", "on"):
        logger.info("Downloads disabled via DISABLE_DOWNLOADS environment variable - skipping download pipeline")
        return
    
    _record_run_start("scheduled")
    try:
        downloaded, failed = download_pipeline()
        _record_run_complete(downloaded=downloaded, failed=failed)
    except Exception as exc:
        logger.error("Full download pipeline error: %s", exc, exc_info=True)


def full_integrity_check() -> None:
    """Full integrity check wrapper for scheduler."""
    integrity_check()


def _record_run_start(run_type: str) -> Optional[int]:
    """Insert a DaemonRun row and return its id."""
    try:
        from src.db import get_session
        from src.models import DaemonRun
        with get_session() as session:
            run = DaemonRun(
                started_at=datetime.now(timezone.utc),
                run_type=run_type,
            )
            session.add(run)
            session.flush()
            run_id = run.id
        return run_id
    except Exception as exc:
        logger.warning("Could not record run start: %s", exc)
        return None


def _record_run_complete(
    run_id: Optional[int] = None,
    downloaded: int = 0,
    failed: int = 0,
    scraped: int = 0,
    requeued: int = 0,
    notes: Optional[str] = None,
) -> None:
    """Update the most recent DaemonRun row with completion stats."""
    try:
        from src.db import get_session
        from src.models import DaemonRun
        with get_session() as session:
            if run_id is not None:
                run = session.get(DaemonRun, run_id)
            else:
                run = (
                    session.query(DaemonRun)
                    .order_by(DaemonRun.started_at.desc())
                    .first()
                )
            if run:
                run.completed_at = datetime.now(timezone.utc)
                run.tracks_downloaded = downloaded
                run.tracks_failed = failed
                run.tracks_scraped = scraped
                run.tracks_requeued = requeued
                if notes:
                    run.notes = notes
    except Exception as exc:
        logger.warning("Could not record run completion: %s", exc)


# ── Startup sequence ──────────────────────────────────────────────────────────

def startup_sequence() -> None:
    """
    Post-DB startup sequence (DB connect + migrations already done in __main__):
      3. Print startup banner
      4. integrity_check()
      5. spotify_incremental_sync()
      6. download_pipeline()
      7. listenbrainz_discovery()
      8. db_backup()
      9. scheduler.start()
    """
    logger.info("=" * 60)
    logger.info("MUSICSTREAM DAEMON v3.0 — starting up")
    logger.info("=" * 60)

    # ── Step 3: Startup banner ────────────────────────────────────────────────
    logger.info("Step 3/9: Printing startup banner…")
    try:
        _print_startup_banner()
    except Exception as exc:
        logger.warning("Startup banner failed (non-fatal): %s", exc)

    # ── Step 4: Integrity check ───────────────────────────────────────────────
    logger.info("Step 4/9: Running integrity check…")
    try:
        integrity_check()
    except Exception as exc:
        logger.error("Integrity check failed (non-fatal): %s", exc)

    # ── Step 5: Spotify incremental sync ─────────────────────────────────────
    logger.info("Step 5/9: Running Spotify incremental sync…")
    try:
        spotify_incremental_sync()
    except Exception as exc:
        logger.error("Spotify sync failed (non-fatal): %s", exc)

    # ── Step 6: Download pipeline ─────────────────────────────────────────────
    logger.info("Step 6/9: Running download pipeline…")
    
    # Check if downloads are disabled (staging/development mode)
    if os.environ.get("DISABLE_DOWNLOADS", "").lower() in ("1", "true", "yes", "on"):
        logger.info("Downloads disabled via DISABLE_DOWNLOADS environment variable - skipping download pipeline")
        # Still record the run with zero downloads
        run_id = _record_run_start("startup")
        _record_run_complete(run_id=run_id, downloaded=0, failed=0)
    else:
        run_id = _record_run_start("startup")
        try:
            downloaded, failed = download_pipeline()
            _record_run_complete(run_id=run_id, downloaded=downloaded, failed=failed)
        except Exception as exc:
            logger.error("Download pipeline failed (non-fatal): %s", exc)

    # ── Step 7: ListenBrainz discovery ────────────────────────────────────────
    logger.info("Step 7/9: Running ListenBrainz discovery…")
    try:
        listenbrainz_discovery()
    except Exception as exc:
        logger.error("ListenBrainz discovery failed (non-fatal): %s", exc)

    # ── Step 8: DB backup ─────────────────────────────────────────────────────
    logger.info("Step 8/9: Running DB backup…")
    try:
        db_backup()
    except Exception as exc:
        logger.error("DB backup failed (non-fatal): %s", exc)

    # ── Step 9: Start scheduler ───────────────────────────────────────────────
    logger.info("Step 9/9: Starting APScheduler…")
    _register_scheduler_jobs()
    scheduler.start()
    logger.info("APScheduler started. Daemon is running.")


# ── Scheduler job registration ────────────────────────────────────────────────

def _register_scheduler_jobs() -> None:
    """
    Register all APScheduler cron jobs:
      - */15 * * * *     — Spotify incremental sync
      - 0 3 * * *        — Full download pipeline
      - 0 4 * * *        — ListenBrainz discovery
      - 0 5 * * 0 (sun)  — Full integrity check
      - 0 5 * * 0 (sun)  — DB backup
    """
    scheduler.add_job(
        spotify_incremental_sync,
        "cron",
        minute="*/15",
        id="spotify_sync",
        replace_existing=True,
    )
    scheduler.add_job(
        full_download_pipeline,
        "cron",
        hour="*/2",
        id="download_pipeline",
        replace_existing=True,
    )
    scheduler.add_job(
        listenbrainz_discovery,
        "cron",
        hour=4,
        id="lb_discovery",
        replace_existing=True,
    )
    scheduler.add_job(
        full_integrity_check,
        "cron",
        day_of_week="sun",
        hour=5,
        id="integrity_check",
        replace_existing=True,
    )
    scheduler.add_job(
        db_backup,
        "cron",
        day_of_week="sun",
        hour=5,
        id="db_backup",
        replace_existing=True,
    )
    logger.info("APScheduler jobs registered: spotify_sync (*/15min), download_pipeline (every 2h), lb_discovery, integrity_check, db_backup")


# ── Flask HTTP control plane ──────────────────────────────────────────────────

_DAEMON_TOKEN: Optional[str] = os.environ.get("DAEMON_API_TOKEN") or None


def _check_auth() -> Optional[tuple]:
    """
    Return a 401 response tuple if DAEMON_API_TOKEN is set and the request
    doesn't supply a matching token. Returns None when auth passes.
    """
    if _DAEMON_TOKEN is None:
        return None  # no token configured — open access (default)
    
    # Check Bearer token header (string slicing for 3.8+ compatibility)
    auth_header = flask_request.headers.get("Authorization", "")
    if auth_header and auth_header.lower().startswith("bearer "):
        provided = auth_header[7:].strip()
    else:
        provided = None
    provided = provided or flask_request.headers.get("X-Daemon-Token")
    if provided != _DAEMON_TOKEN:
        return jsonify({"error": "Unauthorized"}), 401
    return None


@app.get("/")
def index():
    """
    GET /
    Dashboard: progress, active downloads, pending queue, failed tracks, error log tail.
    Refreshes every 60 seconds via meta tag.
    """
    from sqlalchemy import func as sqlfunc, text as sqltext

    # ── DB queries ────────────────────────────────────────────────────────────
    total_tracks = downloaded = pending_count = failed_count = active_count = 0
    progress_pct = 0.0
    active_rows: list = []
    pending_rows: list = []
    failed_rows: list = []
    tier_stats_dict: dict = {}
    last_run_str = "never"
    workers = int(os.environ.get("MAX_CONCURRENT_WORKERS", "4"))
    disable_dl = os.environ.get("DISABLE_DOWNLOADS", "").lower() in ("1", "true", "yes", "on")

    try:
        from src.db import get_session
        from src.models import Track, DownloadAttempt, DaemonRun

        with get_session() as session:
            total_tracks  = session.query(Track).count()
            downloaded     = session.query(Track).filter(Track.status == "downloaded").count()
            pending_count  = session.query(Track).filter(Track.status == "pending").count()
            failed_count   = session.query(Track).filter(Track.status.in_(["failed", "failed_validation", "timed_out"])).count()
            active_count   = session.query(Track).filter(Track.status == "downloading").count()
            progress_pct   = (downloaded / total_tracks * 100) if total_tracks > 0 else 0.0

            # Currently downloading
            active_rows = (
                session.query(Track)
                .filter(Track.status == "downloading")
                .order_by(Track.updated_at.desc())
                .limit(20).all()
            )

            # Pending: worst-stuck (most failed attempts) first
            attempt_count_sub = (
                session.query(
                    DownloadAttempt.track_id,
                    sqlfunc.count(DownloadAttempt.id).label("attempts"),
                )
                .filter(DownloadAttempt.success == False)  # noqa: E712
                .group_by(DownloadAttempt.track_id)
                .subquery()
            )
            pending_rows = (
                session.query(Track, attempt_count_sub.c.attempts)
                .outerjoin(attempt_count_sub, Track.id == attempt_count_sub.c.track_id)
                .filter(Track.status == "pending")
                .order_by(sqlfunc.coalesce(attempt_count_sub.c.attempts, 0).desc())
                .limit(50).all()
            )

            # Failed: with last error message
            last_err_sub = (
                session.query(
                    DownloadAttempt.track_id,
                    sqlfunc.max(DownloadAttempt.attempted_at).label("last_at"),
                )
                .group_by(DownloadAttempt.track_id)
                .subquery()
            )
            failed_rows = (
                session.query(Track, DownloadAttempt.error, DownloadAttempt.method)
                .join(last_err_sub, Track.id == last_err_sub.c.track_id)
                .join(
                    DownloadAttempt,
                    (DownloadAttempt.track_id == Track.id)
                    & (DownloadAttempt.attempted_at == last_err_sub.c.last_at),
                )
                .filter(Track.status.in_(["failed", "failed_validation", "timed_out"]))
                .order_by(Track.updated_at.desc())
                .limit(50).all()
            )

            # Tier stats
            tier_raw = (
                session.query(
                    DownloadAttempt.method,
                    DownloadAttempt.success,
                    sqlfunc.count(DownloadAttempt.id),
                )
                .group_by(DownloadAttempt.method, DownloadAttempt.success)
                .all()
            )
            for method, success, cnt in tier_raw:
                if method not in tier_stats_dict:
                    tier_stats_dict[method] = {"success": 0, "failed": 0, "total": 0}
                tier_stats_dict[method]["success" if success else "failed"] = cnt
                tier_stats_dict[method]["total"] += cnt

            # Last daemon run
            last_run = (
                session.query(DaemonRun)
                .order_by(DaemonRun.started_at.desc())
                .first()
            )
            if last_run and last_run.started_at:
                try:
                    import zoneinfo
                    sgt = zoneinfo.ZoneInfo("Asia/Singapore")
                    last_run_str = last_run.started_at.astimezone(sgt).strftime("%Y-%m-%d %H:%M SGT")
                except Exception:
                    last_run_str = last_run.started_at.strftime("%Y-%m-%d %H:%M UTC")

    except Exception as exc:
        logger.warning("Dashboard DB query failed: %s", exc)

    # ── Error log tail ────────────────────────────────────────────────────────
    error_log_lines: list[str] = []
    try:
        with open(_LOG_DIR / "errors.log", encoding="utf-8", errors="replace") as fh:
            error_log_lines = fh.readlines()[-30:]
    except OSError:
        pass

    # ── Uptime ────────────────────────────────────────────────────────────────
    uptime_s = int(time.time() - _start_time)
    uptime_str = f"{uptime_s // 3600}h {(uptime_s % 3600) // 60}m {uptime_s % 60}s"

    # ── HTML helpers ──────────────────────────────────────────────────────────
    def _esc(s: object) -> str:
        return str(s).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def _badge(status: str) -> str:
        colours = {
            "downloading": "#3498db",
            "pending": "#f39c12",
            "failed": "#e74c3c",
            "failed_validation": "#e74c3c",
            "timed_out": "#c0392b",
            "downloaded": "#27ae60",
        }
        c = colours.get(status, "#95a5a6")
        return f'<span style="background:{c};color:white;padding:2px 8px;border-radius:12px;font-size:11px;font-weight:600">{_esc(status)}</span>'

    # ── Build HTML sections ───────────────────────────────────────────────────
    # Active
    if active_rows:
        active_html = "".join(
            f"<tr><td>{_esc(t.title)}</td><td>{_esc(t.artist)}</td>"
            f"<td>{_esc(t.album or '')}</td></tr>"
            for t in active_rows
        )
    else:
        active_html = "<tr><td colspan='3' style='color:#7f8c8d;text-align:center'>No active downloads right now</td></tr>"

    # Pending
    if pending_rows:
        pending_html = "".join(
            f"<tr><td>{_esc(t.title)}</td><td>{_esc(t.artist)}</td>"
            f"<td>{_esc(t.album or '')}</td>"
            f"<td style='text-align:center;color:{'#e74c3c' if (att or 0)>10 else '#f39c12'};font-weight:bold'>{att or 0}</td></tr>"
            for t, att in pending_rows
        )
    else:
        pending_html = "<tr><td colspan='4' style='color:#7f8c8d;text-align:center'>No pending tracks</td></tr>"

    # Failed
    if failed_rows:
        failed_html = "".join(
            f"<tr><td>{_esc(t.title)}</td><td>{_esc(t.artist)}</td>"
            f"<td>{_esc(t.album or '')}</td>"
            f"<td style='font-size:11px;color:#7f8c8d'>{_esc((err or '')[:80])}</td>"
            f"<td style='font-size:11px'>{_esc(method or '')}</td></tr>"
            for t, err, method in failed_rows
        )
    else:
        failed_html = "<tr><td colspan='5' style='color:#7f8c8d;text-align:center'>No failed tracks</td></tr>"

    # Tier stats
    tier_html = ""
    for method, s in tier_stats_dict.items():
        rate = (s["success"] / s["total"] * 100) if s["total"] > 0 else 0
        col = "#27ae60" if rate >= 80 else "#f39c12" if rate >= 50 else "#e74c3c"
        tier_html += (
            f"<tr><td>{_esc(method)}</td><td>{s['success']}</td>"
            f"<td>{s['failed']}</td><td>{s['total']}</td>"
            f"<td style='color:{col};font-weight:bold'>{rate:.1f}%</td></tr>"
        )
    if not tier_html:
        tier_html = "<tr><td colspan='5' style='color:#7f8c8d;text-align:center'>No download attempts recorded yet</td></tr>"

    # Error log
    log_html = ""
    for line in reversed(error_log_lines):
        line = line.rstrip()
        if not line:
            continue
        col = "#e74c3c" if "ERROR" in line or "CRITICAL" in line else "#f39c12" if "WARNING" in line else "#ecf0f1"
        log_html += f'<div style="font-family:monospace;font-size:11px;padding:2px 0;color:{col};border-bottom:1px solid #2c3e50">{_esc(line)}</div>'
    if not log_html:
        log_html = '<div style="color:#7f8c8d;font-size:12px">No errors logged.</div>'

    disable_banner = (
        '<div style="background:#c0392b;color:white;padding:12px 20px;border-radius:8px;margin-bottom:16px;font-weight:bold">'
        'DOWNLOADS DISABLED (DISABLE_DOWNLOADS is set)</div>'
        if disable_dl else ""
    )

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<meta http-equiv="refresh" content="60">
<title>Musicstream</title>
<style>
*{{box-sizing:border-box;margin:0;padding:0}}
body{{font-family:'Segoe UI',sans-serif;background:#1a1a2e;color:#e0e0e0;padding:16px;max-width:1400px;margin:0 auto}}
h2{{color:#a29bfe;border-bottom:2px solid #6c5ce7;padding-bottom:6px;margin:20px 0 12px}}
.hdr{{background:linear-gradient(135deg,#6c5ce7,#a29bfe);padding:20px 24px;border-radius:10px;margin-bottom:16px;display:flex;justify-content:space-between;align-items:center}}
.hdr h1{{color:white;font-size:22px}}
.hdr small{{color:rgba(255,255,255,.8);font-size:12px}}
.cards{{display:grid;grid-template-columns:repeat(auto-fit,minmax(180px,1fr));gap:12px;margin-bottom:16px}}
.card{{background:#16213e;padding:16px;border-radius:8px;border-left:4px solid #6c5ce7}}
.card .lbl{{font-size:11px;text-transform:uppercase;letter-spacing:1px;color:#a29bfe;margin-bottom:4px}}
.card .val{{font-size:28px;font-weight:700}}
.card .bar{{height:6px;background:#0f3460;border-radius:3px;margin-top:8px;overflow:hidden}}
.card .fill{{height:100%;border-radius:3px;transition:width .4s}}
.sec{{background:#16213e;border-radius:8px;padding:16px;margin-bottom:16px}}
table{{width:100%;border-collapse:collapse;font-size:13px}}
th{{background:#0f3460;color:#a29bfe;padding:8px 10px;text-align:left;font-size:11px;text-transform:uppercase;letter-spacing:.5px}}
td{{padding:7px 10px;border-bottom:1px solid #0f3460;vertical-align:top}}
tr:hover td{{background:#0f3460}}
.actions{{display:flex;gap:8px;flex-wrap:wrap;margin-bottom:16px}}
.btn{{padding:8px 16px;border:none;border-radius:6px;cursor:pointer;font-weight:600;font-size:13px;text-decoration:none;display:inline-block}}
.btn-reload{{background:#6c5ce7;color:white}}
.btn-sec{{background:#0f3460;color:#a29bfe;border:1px solid #6c5ce7}}
.logbox{{background:#0a0a1a;border-radius:6px;padding:12px;max-height:300px;overflow-y:auto}}
</style>
</head>
<body>

<div class="hdr">
  <div>
    <h1>Musicstream</h1>
    <small>Uptime: {uptime_str} &nbsp;|&nbsp; Last run: {_esc(last_run_str)} &nbsp;|&nbsp; Workers: {workers} &nbsp;|&nbsp; Page refreshes every 60s</small>
  </div>
  <div style="text-align:right;font-size:13px;color:rgba(255,255,255,.8)">
    {datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")}
  </div>
</div>

{disable_banner}

<div class="actions">
  <button class="btn btn-reload" onclick="location.reload()">Refresh Now</button>
  <a class="btn btn-sec" href="/metrics">Tier Metrics (JSON)</a>
  <a class="btn btn-sec" href="/status">Run History (JSON)</a>
  <a class="btn btn-sec" href="/api/coverage">Coverage (JSON)</a>
  <a class="btn btn-sec" href="/api/artwork-report">Artwork Report</a>
  <a class="btn btn-sec" href="/docs">API Docs</a>
</div>

<!-- Progress bar -->
<div class="cards">
  <div class="card">
    <div class="lbl">Total library</div>
    <div class="val">{total_tracks:,}</div>
  </div>
  <div class="card" style="border-color:#27ae60">
    <div class="lbl">Downloaded</div>
    <div class="val" style="color:#27ae60">{downloaded:,}</div>
    <div class="bar"><div class="fill" style="width:{min(progress_pct,100):.1f}%;background:#27ae60"></div></div>
    <div style="font-size:11px;color:#7f8c8d;margin-top:4px">{progress_pct:.1f}% complete</div>
  </div>
  <div class="card" style="border-color:#3498db">
    <div class="lbl">Downloading now</div>
    <div class="val" style="color:#3498db">{active_count}</div>
  </div>
  <div class="card" style="border-color:#f39c12">
    <div class="lbl">Pending</div>
    <div class="val" style="color:#f39c12">{pending_count:,}</div>
    <div class="bar"><div class="fill" style="width:{min((pending_count/total_tracks*100) if total_tracks else 0,100):.1f}%;background:#f39c12"></div></div>
  </div>
  <div class="card" style="border-color:#e74c3c">
    <div class="lbl">Failed</div>
    <div class="val" style="color:#e74c3c">{failed_count:,}</div>
    <div class="bar"><div class="fill" style="width:{min((failed_count/total_tracks*100) if total_tracks else 0,100):.1f}%;background:#e74c3c"></div></div>
  </div>
</div>

<!-- Active downloads -->
<div class="sec">
  <h2>Currently Downloading ({active_count})</h2>
  <table>
    <tr><th>Title</th><th>Artist</th><th>Album</th></tr>
    {active_html}
  </table>
</div>

<!-- Pending queue -->
<div class="sec">
  <h2>Pending Queue — top 50 by failed attempts (stuck tracks first)</h2>
  <table>
    <tr><th>Title</th><th>Artist</th><th>Album</th><th>Failed attempts</th></tr>
    {pending_html}
  </table>
</div>

<!-- Failed tracks -->
<div class="sec">
  <h2>Failed Tracks — top 50 most recent</h2>
  <table>
    <tr><th>Title</th><th>Artist</th><th>Album</th><th>Last error</th><th>Last method</th></tr>
    {failed_html}
  </table>
  <div style="margin-top:10px;font-size:12px;color:#7f8c8d">
    <a href="/admin/reset-failed" style="color:#f39c12" onclick="return confirm('Reset ALL failed tracks to pending?')">
      POST /admin/reset-failed
    </a> — re-queue all failed tracks
  </div>
</div>

<!-- Tier performance -->
<div class="sec">
  <h2>Download Tier Performance (all-time)</h2>
  <table>
    <tr><th>Tier / Method</th><th>Success</th><th>Failed</th><th>Total</th><th>Rate</th></tr>
    {tier_html}
  </table>
</div>

<!-- Error log tail -->
<div class="sec">
  <h2>Error Log Tail (last 30 lines)</h2>
  <div class="logbox">
    {log_html}
  </div>
</div>

</body>
</html>"""
    return html, 200


@app.get("/api/progress")
def progress():
    """
    GET /api/progress
    Returns real-time JSON progress data for dashboard updates.
    """
    try:
        from src.db import get_session
        from src.models import Track, DownloadAttempt
        import os
        
        with get_session() as session:
            # Track statistics
            total_tracks = session.query(Track).count()
            downloaded = session.query(Track).filter(Track.status == "downloaded").count()
            pending = session.query(Track).filter(Track.status == "pending").count()
            failed = session.query(Track).filter(Track.status.in_(["failed", "failed_validation", "timed_out"])).count()
            
            # Recent downloads
            recent_downloads = session.query(DownloadAttempt).order_by(
                DownloadAttempt.attempted_at.desc()
            ).limit(20).all()
            
            # Download rate calculation
            recent_count = session.query(DownloadAttempt).filter(
                DownloadAttempt.attempted_at > datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)
            ).count()
            
            # Get current worker count
            current_workers = int(os.environ.get("MAX_CONCURRENT_WORKERS", "6"))
            
            # Calculate estimated completion time
            if downloaded > 0:
                # Simple estimate: based on today's downloads
                daily_rate = recent_count
                remaining = pending
                days_left = remaining / daily_rate if daily_rate > 0 else None
            else:
                days_left = None
            
        return jsonify({
            "status": "ok",
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "tracks": {
                "total": total_tracks,
                "downloaded": downloaded,
                "pending": pending,
                "failed": failed,
                "completion_pct": round((downloaded / total_tracks * 100), 2) if total_tracks > 0 else 0,
            },
            "performance": {
                "workers": current_workers,
                "today_downloads": recent_count,
                "estimated_days_remaining": days_left,
            },
            "recent_activity": [
                {
                    "method": d.method,
                    "success": d.success,
                    "timestamp": d.attempted_at.isoformat(),
                } for d in recent_downloads
            ],
            "uptime_s": round(time.time() - _start_time, 1),
        }), 200
        
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }), 500


@app.post("/api/refresh-artwork")
def refresh_artwork():
    """
    POST /api/artwork-refresh
    Refresh artwork for tracks based on mode parameter.
    
    Query params:
    - mode: 'missing' (only tracks without artwork) or 'all' (all tracks)
    - limit: max number of tracks to process (default: 10)
    - dry_run: if '1', only report what would be done without actually doing it
    
    Returns summary of refreshed tracks with source breakdown.
    """
    try:
        # Get query parameters
        mode = flask_request.args.get("mode", "missing")
        limit = int(flask_request.args.get("limit", "10"))
        dry_run = flask_request.args.get("dry_run", "false").lower() == "true"
        
        # Validate mode
        if mode not in ["missing", "all"]:
            return jsonify({
                "status": "error",
                "error": f"Invalid mode '{mode}'. Must be 'missing' or 'all'",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }), 400
        
        # Validate limit
        if limit < 1 or limit > 1000:
            return jsonify({
                "status": "error",
                "error": "Limit must be between 1 and 1000",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }), 400
        
        import requests
        from src.db import get_session
        from src.ingestion.artwork_checker import check_embedded_artwork, extract_first_artwork
        from src.models import Track
        from pathlib import Path
        
        with get_session() as session:
            # Query target tracks
            query = session.query(Track)
            
            if mode == "missing":
                query = query.filter(
                    (Track.cover_art_url.is_(None)) | (Track.cover_art_url == "")
                )
            
            # Apply limit
            tracks = query.limit(limit).all()
            
            if not tracks:
                return jsonify({
                    "status": "ok",
                    "message": "No tracks found to refresh",
                    "refreshed": 0,
                    "dry_run": dry_run,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                }), 200
            
            refreshed = 0
            errors = []
            source_breakdown = {
                "spotify": 0,
                "musicbrainz": 0, 
                "embedded": 0,
                "failed": 0
            }
            
            for track in tracks:
                try:
                    if not track.file_path or not Path(track.file_path).exists():
                        logger.debug("Skipping track %d: no file path or file missing", track.id)
                        source_breakdown["failed"] += 1
                        continue
                    
                    # Check if artwork refresh is needed
                    has_embedded = False
                    try:
                        has_embedded = check_embedded_artwork(track.file_path)
                    except Exception as e:
                        logger.debug("Artwork check failed for track %d: %s", track.id, e)
                    
                    has_db_url = bool(track.cover_art_url and track.cover_art_url != "")
                    
                    # Skip if both present (unless mode='all' and dry_run)
                    if mode == "missing" and has_embedded and has_db_url:
                        logger.debug("Skipping track %d: already has artwork embedded and in DB", track.id)
                        continue
                    
                    if not dry_run:
                        # Priority 1: Try Spotify existing cover_art_url
                        artwork_data = None
                        source = None
                        
                        if track.cover_art_url and track.cover_art_url != "":
                            try:
                                resp = requests.get(track.cover_art_url, timeout=10)
                                if resp.status_code == 200 and resp.content:
                                    artwork_data = resp.content
                                    source = "spotify"
                            except Exception as e:
                                logger.debug("Failed to fetch from Spotify: %s", e)
                        
                        # Priority 2: Try MusicBrainz (if we had release ID) - placeholder
                        if not artwork_data and track.mb_release_id:
                            try:
                                # Try Cover Art Archive as fallback
                                caa_url = f"https://coverartarchive.org/release/{track.mb_release_id}/front-250"
                                resp = requests.get(caa_url, timeout=10)
                                if resp.status_code == 200 and resp.content:
                                    artwork_data = resp.content
                                    source = "musicbrainz"
                            except Exception as e:
                                logger.debug("Failed to fetch from MusicBrainz: %s", e)
                        
                        # Priority 3: Try extracting from embedded artwork in the file itself
                        if not artwork_data and has_embedded:
                            artwork_data = extract_first_artwork(track.file_path)
                            if artwork_data:
                                source = "embedded"
                        
                        # If we got artwork, write it to the file
                        if artwork_data:
                            try:
                                _write_artwork_track(track.file_path, artwork_data)
                                
                                # Update DB
                                if source:
                                    track.cover_art_source = f"refresh_{source}"
                                
                                session.commit()
                                refreshed += 1
                                source_breakdown[source] += 1
                                logger.info("Refreshed artwork for track %d ('%s') from %s", 
                                           track.id, track.title, source)
                            except Exception as e:
                                logger.warning("Failed to write artwork to file %s for track %d: %s", 
                                               track.file_path, track.id, e)
                                source_breakdown["failed"] += 1
                                errors.append({"track_id": track.id, "title": track.title, "error": str(e)})
                        else:
                            logger.debug("No artwork found for track %d", track.id)
                            source_breakdown["failed"] += 1
                    else:
                        # Dry run mode - just report
                        refreshed += 1
                        if has_db_url:
                            source_breakdown["spotify"] += 1
                        elif has_embedded:
                            source_breakdown["embedded"] += 1
                        else:
                            source_breakdown["musicbrainz"] += 1
                
                except Exception as e:
                    logger.error("Error processing track %d ('%s'): %s", track.id, track.title, e)
                    errors.append({"track_id": track.id, "title": track.title, "error": str(e)})
                    source_breakdown["failed"] += 1
        
        return jsonify({
            "status": "ok",
            "summary": {
                "refreshed": refreshed,
                "processed": len(tracks),
                "errors": len(errors),
            },
            "source_breakdown": source_breakdown,
            "errors": errors[:10],  # Limit error details
            "dry_run": dry_run,
            "message": f"{'Dry run ' + str(dry_run) + ' - ' if dry_run else ''}Processed {len(tracks)} tracks, refreshed {refreshed} artwork",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }), 200
        
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e),
            "refreshed": 0,
            "dry_run": False, 
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }), 500


def _write_artwork_track(file_path: str, artwork_data: bytes) -> None:
    """Write artwork data to a track file, preserving existing tags."""
    try:
        from mutagen import File as AudioFile
        ext = Path(file_path).suffix.lower()
        
        audio = AudioFile(file_path)
        
        if ext == ".mp3":
            from mutagen.id3 import ID3, ID3NoHeaderError
            try:
                audio = ID3(file_path)
            except ID3NoHeaderError:
                audio = ID3()
            
            # Remove existing APIC frames
            audio.delall("APIC")
            # Add new artwork
            from mutagen.id3 import APIC
            audio["APIC"] = APIC(
                encoding=3,
                mime_type="image/jpeg",
                type=3,  # Cover (front)
                desc="Cover",
                data=artwork_data
            )
            audio.save()
            
        elif ext == ".flac":
            from mutagen.flac import FLAC, Picture
            audio = FLAC(file_path)
            # Remove all pictures
            audio.clear_pictures()
            # Add new artwork
            pic = Picture()
            pic.type = 3  # Cover (front) 
            pic.desc = "Cover"
            pic.data = artwork_data
            audio.add_picture(pic)
            audio.save()
            
        elif ext in (".m4a", ".mp4", ".aac"):
            from mutagen.mp4 import MP4
            from mutagen.mp4 import MP4Cover
            audio = MP4(file_path)
            # Remove existing cover
            if "covr" in audio:
                del audio["covr"]
            # Add new artwork
            audio["covr"] = [MP4Cover(artwork_data, imageformat=MP4Cover.FORMAT_JPEG)]
            audio.save()
            
        else:
            logger.warning("Unsupported format %r for artwork writing", ext)
            
    except Exception as e:
        logger.error("Failed to write artwork to %s: %s", file_path, e)


@app.get("/api/artwork-report")
def artwork_report():
    """
    GET /api/artwork-report
    Report on artwork coverage across the library.
    
    Returns:
        - Count of tracks with/without cover_art_url in DB
        - Count of tracks without embedded artwork in files
        - Aggregation by album and artist
        - Top missing albums/artists
    """
    try:
        from src.db import get_session
        from src.models import Track
        from src.ingestion.artwork_checker import check_embedded_artwork
        from sqlalchemy import func
        from pathlib import Path
        
        with get_session() as session:
            # DB-level artwork stats
            total_tracks = session.query(Track).count()
            tracks_with_cover_url = session.query(Track).filter(
                Track.cover_art_url.isnot(None),
                Track.cover_art_url != ""
            ).count()
            tracks_without_cover_url = total_tracks - tracks_with_cover_url
            
            # File-level artwork check (downloaded tracks only)
            downloaded_tracks = session.query(Track).filter(
                Track.file_path.isnot(None),
                Track.file_path != ""
            ).all()
            
            tracks_without_embedded = 0
            checked_files = 0
            
            # Check a sample for embedded artwork (avoid full scan on large libraries)
            sample_size = min(100, len(downloaded_tracks))
            sample_tracks = downloaded_tracks[:sample_size]
            
            for track in sample_tracks:
                if track.file_path and Path(track.file_path).exists():
                    checked_files += 1
                    if not check_embedded_artwork(track.file_path):
                        tracks_without_embedded += 1
            
            # Estimate total without embedded art based on sample
            if checked_files > 0:
                estimated_without_embedded = int(
                    (tracks_without_embedded / checked_files) * len(downloaded_tracks)
                )
            else:
                estimated_without_embedded = 0
            
            # Missing by album
            missing_by_album = session.query(
                Track.album,
                Track.artist,
                func.count(Track.id).label("missing_count")
            ).filter(
                (Track.cover_art_url.is_(None)) | (Track.cover_art_url == "")
            ).group_by(Track.album, Track.artist).order_by(
                func.count(Track.id).desc()
            ).limit(10).all()
            
            # Missing by artist
            missing_by_artist = session.query(
                Track.artist,
                func.count(Track.id).label("missing_count")
            ).filter(
                (Track.cover_art_url.is_(None)) | (Track.cover_art_url == "")
            ).group_by(Track.artist).order_by(
                func.count(Track.id).desc()
            ).limit(10).all()
            
            # Coverage statistics
            cover_url_coverage = (tracks_with_cover_url / total_tracks * 100) if total_tracks > 0 else 0
            embedded_coverage = ((len(downloaded_tracks) - estimated_without_embedded) / len(downloaded_tracks) * 100) if downloaded_tracks else 0
            
        return jsonify({
            "status": "ok",
            "database": {
                "total_tracks": total_tracks,
                "tracks_with_cover_art_url": tracks_with_cover_url,
                "tracks_without_cover_art_url": tracks_without_cover_url,
                "coverage_percentage": round(cover_url_coverage, 2),
            },
            "embedded_artwork": {
                "sample_checked": checked_files,
                "sample_without_embedded": tracks_without_embedded,
                "estimated_total_without_embedded": estimated_without_embedded,
                "total_downloaded_tracks": len(downloaded_tracks),
                "coverage_percentage": round(embedded_coverage, 2),
            },
            "missing_by_album": [
                {
                    "album": row.album,
                    "artist": row.artist,
                    "missing_count": row.missing_count
                } for row in missing_by_album
            ],
            "missing_by_artist": [
                {
                    "artist": row.artist,
                    "missing_count": row.missing_count
                } for row in missing_by_artist
            ],
            "summary": {
                "total_missing_albums": len(missing_by_album),
                "total_missing_artists": len(missing_by_artist),
                "artwork_health": "good" if cover_url_coverage > 90 else "fair" if cover_url_coverage > 70 else "poor"
            },
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }), 200
        
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }), 500


@app.post("/admin/validate-invalid-tracks")
def validate_invalid_tracks():
    """
    POST /admin/validate-invalid-tracks
    Query tracks with empty artist/album and validate against Spotify API.
    
    Returns summary: checked/updated/marked_not_found/errors.
    """
    from src.db import get_session
    from src.models import Track
    from src.ingestion.scraper import SpotifyScraper
    
    # Auth check
    if (auth_err := _check_auth()) is not None:
        return auth_err
    
    checked = 0
    updated = 0
    marked_not_found = 0
    errors = []
    
    try:
        # Initialize Spotify scraper
        client_id = os.environ.get("SPOTIFY_CLIENT_ID")
        if not client_id:
            return jsonify({
                "status": "error",
                "error": "SPOTIFY_CLIENT_ID not configured",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }), 500

        scraper = SpotifyScraper(client_id=client_id)
        
        with get_session() as session:
            # Find tracks with empty artist/album
            invalid_tracks = session.query(Track).filter(
                (Track.artist.is_(None)) | (Track.artist == "") |
                (Track.album.is_(None)) | (Track.album == "")
            ).all()
            
            logger.info("Found %d invalid tracks for validation", len(invalid_tracks))
            
            for track in invalid_tracks:
                checked += 1
                try:
                    if not track.spotify_id:
                        logger.warning("Track %d has no spotify_id, marking as not found", track.id)
                        track.status = "failed"
                        marked_not_found += 1
                        continue
                    
                    # Query Spotify API for track metadata
                    spotify_track = scraper.sp.track(f"spotify:track:{track.spotify_id}")
                    
                    if spotify_track:
                        # Update metadata from Spotify
                        track.artist = spotify_track['artists'][0]['name'] if spotify_track['artists'] else track.artist or ""
                        track.album = spotify_track['album']['name'] if spotify_track['album'] else track.album or ""
                        track.title = spotify_track['name'] or track.title
                        track.spotify_album_id = spotify_track['album']['id'] if spotify_track['album'] else None
                        
                        updated += 1
                        logger.info("Updated track %d: '%s' by '%s'", track.id, track.title, track.artist)
                    else:
                        # Track not found on Spotify
                        track.status = "failed"
                        marked_not_found += 1
                        logger.warning("Track %d not found on Spotify: %s", track.id, track.spotify_id)
                        
                except Exception as e:
                    error_msg = f"Track {track.id}: {str(e)}"
                    errors.append(error_msg)
                    logger.error("Error validating track %d: %s", track.id, e)
            
            session.commit()
            
        return jsonify({
            "status": "ok",
            "summary": {
                "checked": checked,
                "updated": updated,
                "marked_not_found": marked_not_found,
                "errors": len(errors),
            },
            "errors": errors[:10],  # Limit error details
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }), 200
        
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e),
            "checked": checked,
            "updated": updated,
            "marked_not_found": marked_not_found,
            "errors": errors,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }), 500


@app.post("/admin/cleanup-invalid-tracks")
def cleanup_invalid_tracks():
    """
    POST /admin/cleanup-invalid-tracks
    Delete tracks marked as invalid (status='failed') with empty artist/album.
    
    Requires auth if DAEMON_API_TOKEN is set.
    Returns deletion count.
    """
    from src.db import get_session
    from src.models import Track
    
    # Auth check
    if (auth_err := _check_auth()) is not None:
        return auth_err
    
    deleted_count = 0
    
    try:
        with get_session() as session:
            # Only delete tracks explicitly marked as failed with empty artist/album
            tracks_to_delete = session.query(Track).filter(
                Track.status == "failed",
                (Track.artist.is_(None)) | (Track.artist == "") |
                (Track.album.is_(None)) | (Track.album == "")
            ).all()
            
            deleted_count = len(tracks_to_delete)
            
            for track in tracks_to_delete:
                logger.info("Deleting invalid track %d: '%s'", track.id, track.title)
                session.delete(track)
            
            session.commit()
            
        return jsonify({
            "status": "ok",
            "deleted": deleted_count,
            "message": f"Deleted {deleted_count} invalid tracks",
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }), 200
        
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e),
            "deleted": deleted_count,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }), 500




@app.get("/api/coverage")
def coverage():
    """
    GET /api/coverage
    Coverage verification: checks disk vs database completeness.
    
    Returns:
        - Count of unique artists on disk
        - Count of unique albums on disk
        - Count of unique artists in DB
        - Count of unique albums in DB
        - Coverage percentage
        - Files matching DB records
    """
    try:
        from src.db import get_session
        from src.models import Track
        from pathlib import Path
        from sqlalchemy import func
        
        with get_session() as session:
            # DB stats
            db_total_tracks = session.query(Track).count()
            db_unique_artists = session.query(func.count(func.distinct(Track.artist))).scalar()
            db_unique_albums = session.query(func.count(func.distinct(Track.album))).scalar()
            db_downloaded = session.query(Track).filter(Track.file_path.isnot(None)).count()
            
            # Disk stats
            media_dir = Path("/media")
            if media_dir.exists():
                artist_folders = [d for d in media_dir.iterdir() if d.is_dir() and not d.name.startswith(".")]
                unique_artists_on_disk = len(artist_folders)
                
                total_albums_on_disk = 0
                for artist_dir in artist_folders:
                    album_dirs = [d for d in artist_dir.iterdir() if d.is_dir()]
                    total_albums_on_disk += len(album_dirs)
            else:
                unique_artists_on_disk = 0
                total_albums_on_disk = 0
            
            # Coverage calculation
            artist_coverage = (db_unique_artists / unique_artists_on_disk * 100) if unique_artists_on_disk > 0 else 100
            track_coverage = (db_downloaded / db_total_tracks * 100) if db_total_tracks > 0 else 0
            
            return jsonify({
                "status": "ok",
                "database": {
                    "total_tracks": db_total_tracks,
                    "downloaded_tracks": db_downloaded,
                    "unique_artists": db_unique_artists,
                    "unique_albums": db_unique_albums,
                    "completion_pct": round((db_downloaded / db_total_tracks * 100), 2) if db_total_tracks > 0 else 0,
                },
                "disk": {
                    "unique_artists": unique_artists_on_disk,
                    "total_albums": total_albums_on_disk,
                },
                "coverage": {
                    "artist_coverage_pct": round(artist_coverage, 2),
                    "track_coverage_pct": round(track_coverage, 2),
                },
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }), 200
            
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }), 500



@app.get("/api/report")
def report():
    """
    GET /api/report
    Returns detailed report of missing and failed tracks/albums (not saved to file).
    
    Returns:
        - List of missing albums (artist, album, track_count)
        - List of failed downloads (track, artist, album, error_count)
        - Retry statistics by tier
    """
    try:
        from src.db import get_session
        from src.models import Track, DownloadAttempt
        from sqlalchemy import func, and_
        
        with get_session() as session:
            # Missing albums (albums with pending tracks)
            missing_albums = (
                session.query(
                    Track.artist, 
                    Track.album, 
                    func.count(Track.id).label("track_count")
                )
                .filter(Track.status == "pending")
                .group_by(Track.artist, Track.album)
                .order_by(Track.artist, Track.album)
                .limit(50)
                .all()
            )
            
            # Failed tracks (tracks with 3+ failed attempts)
            failed_tracks = (
                session.query(
                    Track.id,
                    Track.title, 
                    Track.artist, 
                    Track.album,
                    Track.status,
                    func.count(DownloadAttempt.id).label("attempt_count")
                )
                .join(DownloadAttempt)
                .group_by(Track.id)
                .having(and_(
                    Track.status.in_(["failed", "failed_validation", "timed_out"]),
                    func.count(DownloadAttempt.id) >= 3
                ))
                .order_by(func.count(DownloadAttempt.id).desc())
                .limit(50)
                .all()
            )
            
            # Missing by artist
            missing_by_artist = (
                session.query(Track.artist, func.count().label("missing_count"))
                .filter(Track.status == "pending")
                .group_by(Track.artist)
                .order_by(Track.artist)
                .limit(30)
                .all()
            )
            
            return jsonify({
                "status": "ok",
                "timestamp": datetime.now(timezone.utc).isoformat(),
                "missing_albums": [
                    {
                        "artist": row.artist,
                        "album": row.album,
                        "track_count": row.track_count,
                    } for row in missing_albums
                ],
                "failed_tracks": [
                    {
                        "id": row.id,
                        "title": row.title,
                        "artist": row.artist,
                        "album": row.album,
                        "status": row.status,
                        "attempt_count": row.attempt_count,
                    } for row in failed_tracks
                ],
                "missing_by_artist": [
                    {
                        "artist": row.artist,
                        "missing_count": row.missing_count,
                    } for row in missing_by_artist
                ],
                "summary": {
                    "total_missing_albums": len(missing_albums),
                    "total_failed_tracks": len(failed_tracks),
                    "total_missing_by_artist": len(missing_by_artist),
                },
            }), 200
            
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }), 500



_retag_state: dict = {"running": False, "processed": 0, "updated": 0, "errors": 0, "done": False, "started_at": None}


@app.post("/admin/retag-tracks")
def retag_tracks():
    """
    POST /admin/retag-tracks
    Retroactively apply TCMP/compilation and multi-artist TPE1 tags.
    Runs in background thread; returns immediately. Poll /admin/retag-tracks/status.

    Query params:
      dry_run=1        — report counts only, touch nothing (default)
      scope=all        — all downloaded tracks (default)
      scope=various    — only Various Artists compilations
      scope=multiartist— only tracks with multiple artists
    """
    if (auth_err := _check_auth()) is not None:
        return auth_err

    if _retag_state["running"]:
        return jsonify({
            "status": "already_running",
            "progress": _retag_state,
        }), 409

    dry_run = flask_request.args.get("dry_run", "1") not in ("0", "false", "no")
    scope = flask_request.args.get("scope", "all")

    if dry_run:
        # Dry-run: synchronous quick count, no file I/O
        from src.db import get_session
        from src.models import Track
        with get_session() as session:
            q = session.query(Track).filter(Track.status == "downloaded", Track.file_path.isnot(None))
            if scope == "various":
                q = q.filter(Track.album_artist.ilike("%various artists%"))
            elif scope == "multiartist":
                q = q.filter(Track.artist.contains(", "))
            count = q.count()
        return jsonify({"status": "ok", "dry_run": True, "scope": scope, "would_process": count}), 200

    # Live run: background thread so HTTP returns immediately
    import threading

    def _run():
        _retag_state.update({"running": True, "processed": 0, "updated": 0, "errors": 0, "done": False,
                              "started_at": datetime.now(timezone.utc).isoformat()})
        try:
            from pathlib import Path as _Path
            from src.db import get_session
            from src.models import Track

            # Fetch IDs only — avoids holding 12K ORM objects in memory
            with get_session() as session:
                q = session.query(Track.id).filter(Track.status == "downloaded", Track.file_path.isnot(None))
                if scope == "various":
                    q = q.filter(Track.album_artist.ilike("%various artists%"))
                elif scope == "multiartist":
                    q = q.filter(Track.artist.contains(", "))
                track_ids = [row[0] for row in q.all()]

            # Process each track in its own session so SHA256 is committed
            for track_id in track_ids:
                with get_session() as session:
                    track = session.get(Track, track_id)
                    if not track or not track.file_path or not _Path(track.file_path).exists():
                        continue
                    _retag_state["processed"] += 1
                    try:
                        _retag_file(track)  # modifies file + updates track.file_sha256
                        _retag_state["updated"] += 1
                    except Exception as exc:
                        _retag_state["errors"] += 1
                        logger.warning("retag failed for track %d: %s", track_id, exc)

            logger.info("retag complete: processed=%d updated=%d errors=%d",
                        _retag_state["processed"], _retag_state["updated"], _retag_state["errors"])
        except Exception as exc:
            logger.error("retag background thread failed: %s", exc)
        finally:
            _retag_state["running"] = False
            _retag_state["done"] = True

    threading.Thread(target=_run, daemon=True, name="retag").start()
    return jsonify({"status": "started", "scope": scope, "poll": "/admin/retag-tracks/status"}), 202


@app.get("/admin/retag-tracks/status")
def retag_tracks_status():
    """GET /admin/retag-tracks/status — poll live retag progress."""
    return jsonify(_retag_state), 200


def _retag_file(track) -> None:
    """Apply TCMP and multi-artist tags; update track.file_sha256 in DB."""
    from pathlib import Path as _P
    ext = _P(track.file_path).suffix.lower()

    if ext == ".mp3":
        from mutagen.id3 import ID3, ID3NoHeaderError, TPE1, TCMP
        try:
            audio = ID3(track.file_path)
        except ID3NoHeaderError:
            return
        artists = [a.strip() for a in (track.artist or "").split(", ") if a.strip()]
        if artists:
            audio["TPE1"] = TPE1(encoding=3, text=artists)
        album_artist = track.album_artist or track.artist or ""
        if album_artist.lower() == "various artists":
            audio["TCMP"] = TCMP(encoding=3, text="1")
        audio.save(v2_version=3)

    elif ext == ".flac":
        from mutagen.flac import FLAC
        audio = FLAC(track.file_path)
        artists = [a.strip() for a in (track.artist or "").split(", ") if a.strip()]
        if artists:
            audio["artist"] = artists
        album_artist = track.album_artist or track.artist or ""
        if album_artist.lower() == "various artists":
            audio["compilation"] = ["1"]
        audio.save()

    elif ext in (".m4a", ".mp4", ".aac"):
        from mutagen.mp4 import MP4
        audio = MP4(track.file_path)
        artists = [a.strip() for a in (track.artist or "").split(", ") if a.strip()]
        if artists:
            audio["\xa9ART"] = artists
        album_artist = track.album_artist or track.artist or ""
        if album_artist.lower() == "various artists":
            audio["cpil"] = [True]
        audio.save()

    else:
        return  # unsupported format — nothing written, hash unchanged

    # Update the stored SHA-256 so integrity check won't flag the modified file
    from src.utils import compute_sha256
    track.file_sha256 = compute_sha256(track.file_path)


@app.post("/admin/recover-pending")
def recover_pending():
    """
    POST /admin/recover-pending
    For pending tracks whose file_path was cleared by the integrity checker
    (due to retag SHA256 mismatch), reconstruct the expected path and restore
    status=downloaded if the file still exists on disk.
    """
    if (auth_err := _check_auth()) is not None:
        return auth_err

    import re as _re
    from pathlib import Path as _Path
    from src.db import get_session
    from src.models import Track, TrackStatus
    from src.utils import compute_sha256

    _FORBIDDEN_RE = _re.compile(r'[<>:"/\\|?*]')

    def _sanitize(name: str) -> str:
        s = _FORBIDDEN_RE.sub("_", name)[:200].strip(". ")
        return s

    media_dir = os.environ.get("MEDIA_DIR", "/media")
    recovered = skipped = errors = 0

    try:
        with get_session() as session:
            # Candidates: pending, no file_path, have metadata (were downloaded before)
            candidates = (
                session.query(Track)
                .filter(
                    Track.status == TrackStatus.PENDING.value,
                    Track.file_path.is_(None),
                    Track.album_artist.isnot(None),
                    Track.title.isnot(None),
                    Track.format.isnot(None),
                )
                .all()
            )

            for track in candidates:
                try:
                    aa = _sanitize(track.album_artist or track.artist or "Unknown Artist")
                    alb = track.album or "Unknown Album"
                    alb_folder = _sanitize(f"{alb} ({track.year})") if track.year else _sanitize(alb)
                    ext = f".{track.format}"
                    if track.track_number is not None:
                        nn = str(track.track_number).zfill(2)
                        fname = _sanitize(f"{nn} - {track.title}") + ext
                    else:
                        fname = _sanitize(track.title) + ext

                    expected = os.path.join(media_dir, aa, alb_folder, fname)

                    if _Path(expected).exists():
                        sha = compute_sha256(expected)
                        track.file_path = expected
                        track.file_sha256 = sha
                        track.status = TrackStatus.DOWNLOADED.value
                        recovered += 1
                    else:
                        skipped += 1

                except Exception as exc:
                    errors += 1
                    logger.warning("recover-pending: track %d failed: %s", track.id, exc)

        return jsonify({
            "status": "ok",
            "recovered": recovered,
            "left_pending": skipped,
            "errors": errors,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }), 200

    except Exception as exc:
        return jsonify({"status": "error", "error": str(exc)}), 500


@app.get("/docs")
def docs():
    """
    GET /docs
    Returns API documentation for all endpoints.
    """
    docs_html = '''<!DOCTYPE html>\n<html>\n<head>\n    <title>Musicstream API Documentation</title>\n    <style>\n        body { font-family: "Segoe UI", sans-serif; max-width: 900px; margin: 40px auto; padding: 20px; background: #f5f7fa; }\n        h1 { color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }\n        h2 { color: #34495e; border-bottom: 1px solid #ddd; padding-bottom: 5px; margin-top: 30px; }\n        .endpoint { background: white; padding: 20px; border-radius: 8px; margin: 20px 0; border-left: 4px solid #3498db; }\n        .endpoint h3 { margin: 0 0 10px 0; color: #2196F3; }\n        .get { background: #27ae60; color: white; }\n        .post { background: #e74c3c; color: white; }\n    </style>\n</head>\n<body>\n    <h1>Musicstream API Documentation</h1>\n    <p>Base URL: <strong>http://localhost:9079</strong></p>\n    <div class="endpoint">\n        <h3><span class="get">GET</span> /</h3>\n        <p>Interactive web dashboard</p>\n    </div>\n    <div class="endpoint">\n        <h3><span class="get">GET</span> /api/progress</h3>\n        <p>Real-time download progress</p>\n    </div>\n    <div class="endpoint">\n        <h3><span class="get">GET</span> /api/coverage</h3>\n        <p>Disk vs database completeness</p>\n    </div>\n    <div class="endpoint">\n        <h3><span class="get">GET</span> /api/report</h3>\n        <p>Missing and failed tracks report</p>\n    </div>\n    <div class="endpoint">\n        <h3><span class="get">GET</span> /api/artwork-report</h3>\n        <p>Artwork coverage report (DB URLs, embedded files, aggregation by album/artist)</p>\n    </div>\n    <div class="endpoint">\n        <h3><span class="post">POST</span> /api/refresh-artwork</h3>\n        <p>Refresh artwork for tracks (mode=missing or mode=all, limit=N). Supports dry_run mode for testing. Returns breakdown by source (spotify/musicbrainz/embedded)</p>\n    </div>\n    <div class="endpoint">\n        <h3><span class="get">GET</span> /health, /status, /metrics</h3>\n        <p>Health checks and metrics</p>\n    </div>\n    <h2>Control Endpoints (requires authentication if DAEMON_API_TOKEN is set)</h2>\n    <div class="endpoint">\n        <h3><span class="post">POST</span> /sync</h3>\n        <p>Trigger Spotify sync + download</p>\n    </div>\n    <div class="endpoint">\n        <h3><span class="post">POST</span> /integrity</h3>\n        <p>Run integrity check</p>\n    </div>\n    <div class="endpoint">\n        <h3><span class="post">POST</span> /discover</h3>\n        <p>Fetch ListenBrainz recommendations</p>\n    </div>\n    <div class="endpoint">\n        <h3><span class="post">POST</span> /admin/reset-failed</h3>\n        <p>Reset all failed tracks to pending status for retry</p>\n    </div>\n    <div class="endpoint">\n        <h3><span class="post">POST</span> /admin/validate-invalid-tracks</h3>\n        <p>Validate tracks with empty artist/album against Spotify API. Returns: checked/updated/marked_not_found/errors</p>\n    </div>\n    <div class="endpoint">\n        <h3><span class="post">POST</span> /admin/cleanup-invalid-tracks</h3>\n        <p>Delete tracks marked as invalid (status=failed) with empty artist/album</p>\n    </div>\n</body>\n</html>'''
    return docs_html, 200


@app.post("/admin/reset-failed")
def reset_failed_tracks():
    """
    POST /admin/reset-failed
    Reset all tracks with status='failed', 'failed_validation', or 'missing' back to pending.
    Clears file_path and file_sha256 so they can be re-downloaded.
    """
    from src.db import get_session
    from src.models import Track, TrackStatus
    
    try:
        with get_session() as session:
            # Count tracks to reset before modification
            reset_statuses = [
                TrackStatus.FAILED.value,
                TrackStatus.FAILED_VALIDATION.value,
                TrackStatus.MISSING.value,
            ]
            
            tracks_to_reset = session.query(Track).filter(
                Track.status.in_(reset_statuses)
            ).all()
            
            reset_count = len(tracks_to_reset)
            
            # Reset each track
            for track in tracks_to_reset:
                track.status = TrackStatus.PENDING.value
                track.file_path = None
                track.file_sha256 = None
                # Keep download_attempts for audit
            
            session.commit()
            
            return jsonify({
                "status": "ok",
                "reset_count": reset_count,
                "message": f"Reset {reset_count} failed tracks to pending status",
                "timestamp": datetime.now(timezone.utc).isoformat(),
            }), 200
            
    except Exception as e:
        return jsonify({
            "status": "error",
            "error": str(e),
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }), 500



@app.get("/health")
def health():
    """
    GET /health
    Returns: {"status": "ok", "uptime_s": N, "db_tracks": N}
    """
    uptime_s = time.time() - _start_time
    db_tracks = _get_db_track_count()
    return jsonify({
        "status": "ok",
        "uptime_s": round(uptime_s, 1),
        "db_tracks": db_tracks,
    }), 200


@app.get("/status")
def status():
    """
    GET /status
    Returns the last 5 daemon_runs rows as JSON.
    """
    try:
        from src.db import get_session
        from src.models import DaemonRun
        with get_session() as session:
            runs = (
                session.query(DaemonRun)
                .order_by(DaemonRun.started_at.desc())
                .limit(5)
                .all()
            )
            result = []
            for run in runs:
                result.append({
                    "id": run.id,
                    "run_type": run.run_type,
                    "started_at": run.started_at.isoformat() if run.started_at else None,
                    "completed_at": run.completed_at.isoformat() if run.completed_at else None,
                    "tracks_scraped": run.tracks_scraped,
                    "tracks_downloaded": run.tracks_downloaded,
                    "tracks_failed": run.tracks_failed,
                    "tracks_requeued": run.tracks_requeued,
                    "notes": run.notes,
                })
        return jsonify(result), 200
    except Exception as exc:
        logger.error("/status endpoint error: %s", exc)
        return jsonify({"error": str(exc)}), 500


def _dispatch(fn, job_id: str) -> None:
    """Run *fn* via scheduler if running, otherwise in a daemon thread."""
    import threading
    if scheduler.running:
        scheduler.add_job(fn, id=job_id, replace_existing=True)
    else:
        threading.Thread(target=fn, name=job_id, daemon=True).start()


@app.post("/sync")
def sync():
    """
    POST /sync
    Triggers the full pipeline (Spotify sync + download) immediately.
    Returns 202 Accepted with {"queued": true}.
    """
    if (auth_err := _check_auth()) is not None:
        return auth_err
    try:
        _dispatch(_run_full_pipeline, "manual_sync")
        logger.info("Manual sync dispatched via /sync")
        return jsonify({"queued": True}), 202
    except Exception as exc:
        logger.error("/sync endpoint error: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.post("/integrity")
def integrity():
    """
    POST /integrity
    Triggers an integrity check immediately.
    Returns 202 Accepted with {"queued": true}.
    """
    if (auth_err := _check_auth()) is not None:
        return auth_err
    try:
        _dispatch(integrity_check, "manual_integrity")
        logger.info("Manual integrity check dispatched via /integrity")
        return jsonify({"queued": True}), 202
    except Exception as exc:
        logger.error("/integrity endpoint error: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.post("/discover")
def discover():
    """
    POST /discover
    Triggers ListenBrainz discovery immediately.
    Returns 202 Accepted with {"queued": true}.
    """
    if (auth_err := _check_auth()) is not None:
        return auth_err
    try:
        _dispatch(listenbrainz_discovery, "manual_discover")
        logger.info("Manual LB discovery dispatched via /discover")
        return jsonify({"queued": True}), 202
    except Exception as exc:
        logger.error("/discover endpoint error: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.get("/metrics")
def metrics():
    """
    GET /metrics
    Returns per-tier download stats and success rates from download_attempts table.
    """
    try:
        from src.db import get_session
        from src.models import DownloadAttempt
        from sqlalchemy import case, func
        with get_session() as session:
            rows = (
                session.query(
                    DownloadAttempt.method,
                    func.count(DownloadAttempt.id).label("total"),
                    func.sum(
                        case((DownloadAttempt.success == True, 1), else_=0)  # noqa: E712
                    ).label("successes"),
                )
                .group_by(DownloadAttempt.method)
                .all()
            )

            tier_stats = []
            for row in rows:
                total = row.total or 0
                successes = int(row.successes or 0)
                failures = total - successes
                success_rate = round(successes / total * 100, 1) if total > 0 else 0.0
                tier_stats.append({
                    "method": row.method,
                    "total_attempts": total,
                    "successes": successes,
                    "failures": failures,
                    "success_rate_pct": success_rate,
                })

            # Overall stats
            total_all = sum(t["total_attempts"] for t in tier_stats)
            success_all = sum(t["successes"] for t in tier_stats)
            overall_rate = round(success_all / total_all * 100, 1) if total_all > 0 else 0.0

        return jsonify({
            "per_tier": tier_stats,
            "overall": {
                "total_attempts": total_all,
                "successes": success_all,
                "failures": total_all - success_all,
                "success_rate_pct": overall_rate,
            },
        }), 200
    except Exception as exc:
        logger.error("/metrics endpoint error: %s", exc)
        return jsonify({"error": str(exc)}), 500


@app.post("/backup")
def backup():
    """
    POST /backup
    Runs pg_dump immediately.
    Returns {"path": "...", "size_bytes": N} on success, or 500 on failure.
    """
    if (auth_err := _check_auth()) is not None:
        return auth_err
    try:
        path = db_backup()
        if path is None:
            return jsonify({"error": "pg_dump failed; check daemon.log"}), 500
        size_bytes = Path(path).stat().st_size
        return jsonify({"path": path, "size_bytes": size_bytes}), 200
    except Exception as exc:
        logger.error("/backup endpoint error: %s", exc)
        return jsonify({"error": str(exc)}), 500


# ── Internal: full pipeline for manual trigger ────────────────────────────────

def _run_full_pipeline() -> None:
    """Run Spotify sync + download pipeline (used by /sync endpoint)."""
    run_id = _record_run_start("manual")
    try:
        spotify_incremental_sync()
        
        # Check if downloads are disabled
        if os.environ.get("DISABLE_DOWNLOADS", "").lower() in ("1", "true", "yes", "on"):
            logger.info("Downloads disabled via DISABLE_DOWNLOADS - skipping download in manual sync")
            _record_run_complete(run_id=run_id, downloaded=0, failed=0)
            return
        
        downloaded, failed = download_pipeline()
        _record_run_complete(run_id=run_id, downloaded=downloaded, failed=failed)
    except Exception as exc:
        logger.error("Full pipeline (manual) error: %s", exc, exc_info=True)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import signal
    import threading

    # ── SIGTERM handler — allows Docker `stop` to shut down cleanly ───────────
    def _handle_sigterm(signum, frame):  # type: ignore[misc]
        logger.info("SIGTERM received — shutting down scheduler and exiting")
        if scheduler.running:
            scheduler.shutdown(wait=False)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, _handle_sigterm)

    # ── Steps 1 & 2: DB + migrations — must succeed before Flask starts ───────
    try:
        from src.db import init_db, run_migrations, wait_for_db
        _db_engine = wait_for_db(max_retries=5, backoff_s=5.0)
        init_db(engine=_db_engine)   # reuse engine, avoid second pool
        run_migrations()
        logger.info("DB ready. Starting Flask…")
    except Exception as exc:
        logger.critical("DB init/migration failed: %s — aborting", exc, exc_info=True)
        raise SystemExit(1) from exc

    # ── Steps 3-9 run in background so Flask health endpoint is always up ─────
    def _background_startup() -> None:
        try:
            logger.info("=" * 60)
            logger.info("MUSICSTREAM DAEMON v3.0 — background startup")
            logger.info("=" * 60)

            logger.info("Step 3/9: Printing startup banner…")
            try:
                _print_startup_banner()
            except Exception as exc:
                logger.warning("Startup banner failed (non-fatal): %s", exc)

            logger.info("Step 4/9: Running integrity check…")
            try:
                integrity_check()
            except Exception as exc:
                logger.error("Integrity check failed (non-fatal): %s", exc)

            logger.info("Step 5/9: Running Spotify incremental sync…")
            try:
                spotify_incremental_sync()
            except Exception as exc:
                logger.error("Spotify sync failed (non-fatal): %s", exc)

            logger.info("Step 6/9: Running download pipeline…")
            run_id = _record_run_start("startup")
            try:
                downloaded, failed = download_pipeline()
                _record_run_complete(run_id=run_id, downloaded=downloaded, failed=failed)
            except Exception as exc:
                logger.error("Download pipeline failed (non-fatal): %s", exc)

            logger.info("Step 7/9: Running ListenBrainz discovery…")
            try:
                listenbrainz_discovery()
            except Exception as exc:
                logger.error("ListenBrainz discovery failed (non-fatal): %s", exc)

            logger.info("Step 8/9: Running DB backup…")
            try:
                db_backup()
            except Exception as exc:
                logger.error("DB backup failed (non-fatal): %s", exc)

            logger.info("Step 9/9: Starting APScheduler…")
            _register_scheduler_jobs()
            scheduler.start()
            logger.info("Daemon fully initialised. Scheduler running.")

        except Exception as exc:
            logger.critical("Background startup failed: %s", exc, exc_info=True)

    t = threading.Thread(target=_background_startup, name="startup", daemon=True)
    t.start()

    # Flask runs on main thread — always available for health checks
    app.run(host="0.0.0.0", port=9079, threaded=True)
