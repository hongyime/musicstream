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
from flask import Flask, jsonify

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
        f"[bold cyan]MUSICSTREAM DAEMON v3.0[/bold cyan]",
        "",
        f"Last full run:   {last_run_str}",
        f"Downloaded: {downloaded:>3}  │  Failed: {failed:>4}  │  Requeued: {requeued:>2}",
        f"DB tracks: {db_tracks:>5}  │  Missing: {missing:>3}  │  Corrupt: {corrupt:>2}",
        f"LB recs:   {lb_total:>5}  │  Ingested: {lb_ingested:>2}",
        f"errors.log: {errors_mb:.1f}MB / 5MB",
    ]

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
        with get_session() as session:
            downloaded, failed = orchestrator.download_pending(session)
        logger.info(
            "Download pipeline complete: downloaded=%d failed=%d",
            downloaded, failed,
        )
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

    logger.info("Running pg_dump → %s", backup_path)
    try:
        result = subprocess.run(
            ["pg_dump", database_url, "--file", str(backup_path)],
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
    Full daemon startup sequence:
      1. Connect PostgreSQL (retry 5x, 5s backoff)
      2. alembic upgrade head
      3. Print startup banner (PRD §13.3)
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

    # ── Step 1: Connect to PostgreSQL ─────────────────────────────────────────
    logger.info("Step 1/9: Connecting to PostgreSQL…")
    try:
        from src.db import init_db, run_migrations, wait_for_db
        engine = wait_for_db(max_retries=5, backoff_s=5.0)
        init_db()
        logger.info("PostgreSQL connection established.")
    except Exception as exc:
        logger.critical("Cannot connect to PostgreSQL: %s — aborting startup", exc)
        raise

    # ── Step 2: Alembic upgrade head ──────────────────────────────────────────
    logger.info("Step 2/9: Running Alembic migrations…")
    try:
        run_migrations()
    except Exception as exc:
        logger.critical("Alembic migration failed: %s — aborting startup", exc)
        raise

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
    run_id = _record_run_start("scheduled")
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
        hour=3,
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
    logger.info("APScheduler jobs registered: spotify_sync, download_pipeline, lb_discovery, integrity_check, db_backup")


# ── Flask HTTP control plane ──────────────────────────────────────────────────

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


@app.post("/sync")
def sync():
    """
    POST /sync
    Triggers the full pipeline (Spotify sync + download) immediately.
    Returns 202 Accepted with {"queued": true}.
    """
    try:
        scheduler.add_job(
            _run_full_pipeline,
            id="manual_sync",
            replace_existing=True,
        )
        logger.info("Manual sync queued via /sync")
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
    try:
        scheduler.add_job(
            integrity_check,
            id="manual_integrity",
            replace_existing=True,
        )
        logger.info("Manual integrity check queued via /integrity")
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
    try:
        scheduler.add_job(
            listenbrainz_discovery,
            id="manual_discover",
            replace_existing=True,
        )
        logger.info("Manual LB discovery queued via /discover")
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
        from sqlalchemy import Integer, case, func
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
        downloaded, failed = download_pipeline()
        _record_run_complete(run_id=run_id, downloaded=downloaded, failed=failed)
    except Exception as exc:
        logger.error("Full pipeline (manual) error: %s", exc, exc_info=True)


# ── Entry point ───────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import threading

    # ── Step 1 & 2: DB + migrations must succeed before anything else ─────────
    try:
        from src.db import init_db, run_migrations, wait_for_db
        wait_for_db(max_retries=5, backoff_s=5.0)
        init_db()
        run_migrations()
    except Exception as exc:
        logger.critical("DB init/migration failed: %s — aborting", exc, exc_info=True)
        raise SystemExit(1) from exc

    # ── Start Flask immediately so health checks pass ─────────────────────────
    # Steps 3-9 (banner, integrity, Spotify sync, downloads, LB, backup,
    # scheduler) run in a background thread so the HTTP server is never blocked.
    def _background_startup() -> None:
        try:
            startup_sequence()
        except Exception as exc:
            logger.critical("Startup sequence failed: %s", exc, exc_info=True)

    t = threading.Thread(target=_background_startup, name="startup", daemon=True)
    t.start()

    # Run Flask on port 9079 (threaded for concurrent requests)
    logger.info("Flask starting on 0.0.0.0:9079")
    app.run(host="0.0.0.0", port=9079, threaded=True)
