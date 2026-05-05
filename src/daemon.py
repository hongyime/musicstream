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
    Returns a comprehensive web dashboard with real-time progress monitoring.
    Auto-refreshes every 5 seconds.
    """
    # Get up-to-date statistics
    try:
        from src.db import get_session
        from src.models import Track, DownloadAttempt, DaemonRun
        import json
        
        with get_session() as session:
            # Track statistics
            total_tracks = session.query(Track).count()
            downloaded = session.query(Track).filter(Track.status == "downloaded").count()
            pending = session.query(Track).filter(Track.status == "pending").count()
            failed = session.query(Track).filter(Track.status.in_(["failed", "failed_validation", "timed_out"])).count()
            
            # Recent download attempts
            recent_downloads = session.query(DownloadAttempt).order_by(
                DownloadAttempt.attempted_at.desc()
            ).limit(10).all()
            
            # Download metrics by tier
            tier_stats = session.query(
                DownloadAttempt.method,
                DownloadAttempt.success,
                session.func.count(DownloadAttempt.id)
            ).group_by(DownloadAttempt.method, DownloadAttempt.success).all()
            
            # Progress percentage
            progress_pct = (downloaded / total_tracks * 100) if total_tracks > 0 else 0
            
    except Exception as e:
        # Fallback if DB query fails
        total_tracks, downloaded, pending, failed = 0, 0, 0, 0
        tier_stats = []
        recent_downloads = []
        progress_pct = 0
    
    # Calculate uptime
    uptime_seconds = int(time.time() - _start_time)
    uptime_str = f"{uptime_seconds // 3600}h {(uptime_seconds % 3600) // 60}m {uptime_seconds % 60}s"
    
    # Format tier stats
    tier_stats_dict = {}
    for method, success, count in tier_stats:
        if method not in tier_stats_dict:
            tier_stats_dict[method] = {"success": 0, "failed": 0, "total": 0}
        tier_stats_dict[method]["success" if success else "failed"] = count
        tier_stats_dict[method]["total"] += count
    
    # Format recent downloads for display
    recent_html = ""
    for attempt in recent_downloads:
        status_color = "green" if attempt.success else "red"
        recent_html += f"""
        <tr>
            <td>{attempt.method}</td>
            <td style="color: {status_color}; font-weight: bold;">{"✓" if attempt.success else "✗"}</td>
            <td>{attempt.timestamp.strftime("%H:%M:%S")}</td>
        </tr>
        """
    
    # Format tier stats table
    tier_html = ""
    for method, stats in tier_stats_dict.items():
        success_rate = (stats["success"] / stats["total"] * 100) if stats["total"] > 0 else 0
        tier_color = "green" if success_rate >= 80 else "orange" if success_rate >= 50 else "red"
        tier_html += f"""
        <tr>
            <td>{method}</td>
            <td>{stats["success"]}</td>
            <td>{stats["failed"]}</td>
            <td>{stats["total"]}</td>
            <td style="color: {tier_color}; font-weight: bold;">{success_rate:.1f}%</td>
        </tr>
        """
    
    html = f"""
<!DOCTYPE html>
<html>
<head>
    <title>Musicstream Dashboard</title>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Musicstream Dashboard</title>
    <style>
        body {{ 
            font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif; 
            max-width: 1400px; 
            margin: 0 auto; 
            padding: 20px; 
            background: #f5f7fa;
        }}
        h1 {{ color: #2c3e50; margin-bottom: 5px; }}
        h2 {{ color: #34495e; border-bottom: 3px solid #3498db; padding-bottom: 10px; }}
        .header {{ background: linear-gradient(135deg, #667eea 0%, #764ba2 100%); color: white; padding: 30px; border-radius: 10px; margin-bottom: 30px; }}
        .header h1 {{ color: white; margin: 0; }}
        .header .subtitle {{ opacity: 0.9; margin-top: 10px; }}
        .stats-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(250px, 1fr)); gap: 20px; margin-bottom: 30px; }}
        .stat-card {{ background: white; padding: 25px; border-radius: 10px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); transition: transform 0.2s; }}
        .stat-card:hover {{ transform: translateY(-5px); }}
        .stat-card h3 {{ margin: 0 0 10px 0; color: #7f8c8d; font-size: 14px; text-transform: uppercase; letter-spacing: 1px; }}
        .stat-card .value {{ font-size: 36px; font-weight: bold; color: #2c3e50; margin: 0; }}
        .stat-card .progress {{ height: 8px; background: #ecf0f1; border-radius: 4px; margin-top: 15px; overflow: hidden; }}
        .stat-card .progress-bar {{ height: 100%; background: linear-gradient(90deg, #667eea 0%, #764ba2 100%); transition: width 0.5s; }}
        .log-panel {{ background: white; padding: 20px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        .log-panel h3 {{ margin-top: 0; color: #34495e; }}
        .log-row {{ font-family: 'Courier New', monospace; font-size: 13px; padding: 5px 0; border-bottom: 1px solid #ecf0f1; }}
        .log-success {{ color: #27ae60; }}
        .log-error {{ color: #e74c3c; }}
        table {{ width: 100%; border-collapse: collapse; background: white; border-radius: 10px; overflow: hidden; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        th {{ background: #34495e; color: white; padding: 15px; text-align: left; font-weight: 600; text-transform: uppercase; font-size: 12px; letter-spacing: 0.5px; }}
        td {{ padding: 12px 15px; border-bottom: 1px solid #ecf0f1; }}
        tr:hover {{ background: #f8f9fa; }}
        .status-badge {{ padding: 5px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; text-transform: uppercase; }}
        .status-success {{ background: #27ae60; color: white; }}
        .status-pending {{ background: #f39c12; color: white; }}
        .status-failed {{ background: #e74c3c; color: white; }}
        .quick-actions {{ display: flex; gap: 10px; flex-wrap: wrap; }}
        .action-btn {{ padding: 12px 20px; border: none; border-radius: 5px; cursor: pointer; font-weight: 600; transition: all 0.3s; text-decoration: none; display: inline-block; }}
        .btn-primary {{ background: #3498db; color: white; }}
        .btn-primary:hover {{ background: #2980b9; }}
        .btn-success {{ background: #27ae60; color: white; }}
        .btn-success:hover {{ background: #219a52; }}
        .btn-warning {{ background: #f39c12; color: white; }}
        .btn-warning:hover {{ background: #e67e22; }}
        .section {{ background: white; padding: 25px; border-radius: 10px; margin-bottom: 20px; box-shadow: 0 2px 10px rgba(0,0,0,0.1); }}
        .auto-refresh {{ position: fixed; top: 20px; right: 20px; background: #2c3e50; color: white; padding: 10px 15px; border-radius: 5px; font-size: 12px; animation: pulse 2s infinite; }}
        @keyframes pulse {{ 0% {{ opacity: 1; }} 50% {{ opacity: 0.5; }} 100% {{ opacity: 1; }} }}
        .worker-status {{ display: flex; align-items: center; gap: 10px; }}
        .worker-dot {{ width: 12px; height: 12px; border-radius: 50%; background: #27ae60; animation: blink 1s infinite; }}
        @keyframes blink {{ 0% {{ opacity: 1; }} 50% {{ opacity: 0.3; }} 100% {{ opacity: 1; }} }}
    </style>
</head>
<body>
    <div class="header">
        <h1>🎵 Musicstream Dashboard</h1>
        <div class="subtitle">Real-time Progress Monitoring • Manual Refresh</div>
    </div>
    
    <div style="display: flex; gap: 10px; margin-bottom: 20px;">
        <button onclick="location.reload()" style="
            background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
            color: white;
            border: none;
            padding: 10px 25px;
            font-size: 16px;
            border-radius: 8px;
            cursor: pointer;
            font-weight: bold;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            transition: transform 0.2s;
        " onmouseover="this.style.transform='scale(1.05)'" onmouseout="this.style.transform='scale(1)'">
            🔄 Refresh Dashboard
        </button>
        <a href="/api/coverage" style="
            background: white;
            color: #667eea;
            border: 2px solid #667eea;
            padding: 10px 20px;
            font-size: 16px;
            border-radius: 8px;
            text-decoration: none;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            font-weight: bold;
        " onmouseover="this.style.background='#667eea'; this.style.color='white'" onmouseout="this.style.background='white'; this.style.color='#667eea'">
            📊 Coverage Report
        </a>
        <a href="/api/report" style="
            background: white;
            color: #e74c3c;
            border: 2px solid #e74c3c;
            padding: 10px 20px;
            font-size: 16px;
            border-radius: 8px;
            text-decoration: none;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            font-weight: bold;
        " onmouseover="this.style.background='#e74c3c'; this.style.color='white'" onmouseout="this.style.background='white'; this.style.color='#e74c3c'">
            📋 Missing/Failed Report
        </a>
        <a href="/docs" style="
            background: white;
            color: #27ae60;
            border: 2px solid #27ae60;
            padding: 10px 20px;
            font-size: 16px;
            border-radius: 8px;
            text-decoration: none;
            box-shadow: 0 4px 6px rgba(0,0,0,0.1);
            font-weight: bold;
        " onmouseover="this.style.background='#27ae60'; this.style.color='white'" onmouseout="this.style.background='white'; this.style.color='#27ae60'">
            📚 API Docs
        </a>
    </div>
    
    <div class="stats-grid">
        <div class="stat-card">
            <h3>📊 Total Library</h3>
            <p class="value">{total_tracks:,}</p>
            <div class="progress">
                <div class="progress-bar" style="width: 100%;"></div>
            </div>
        </div>
        
        <div class="stat-card">
            <h3>✅ Downloaded</h3>
            <p class="value" style="color: #27ae60;">{downloaded:,}</p>
            <div class="progress">
                <div class="progress-bar" style="width: {progress_pct}%; background: #27ae60;"></div>
            </div>
            <small style="color: #7f8c8d;">{progress_pct:.1f}% complete</small>
        </div>
        
        <div class="stat-card">
            <h3>⏳ Pending</h3>
            <p class="value" style="color: #f39c12;">{pending:,}</p>
            <div class="progress">
                <div class="progress-bar" style="width: {(pending/total_tracks*100) if total_tracks>0 else 0}%; background: #f39c12;"></div>
            </div>
        </div>
        
        <div class="stat-card">
            <h3>❌ Failed</h3>
            <p class="value" style="color: #e74c3c;">{failed:,}</p>
            <div class="progress">
                <div class="progress-bar" style="width: {(failed/total_tracks*100) if total_tracks>0 else 0}%; background: #e74c3c;"></div>
            </div>
        </div>
    </div>
    
    <div class="section">
        <h2>⚙️ System Status</h2>
        <table>
            <tr>
                <th>Metric</th>
                <th>Value</th>
            </tr>
            <tr>
                <td>Uptime</td>
                <td>{uptime_str}</td>
            </tr>
            <tr>
                <td>Worker Configuration</td>
                <td>
                    <div class="worker-status">
                        <div class="worker-dot"></div>
                        <strong>12 Workers</strong> (MAX_CONCURRENT=12)
                    </div>
                </td>
            </tr>
            <tr>
                <td>Download Pipeline</td>
                <td><span class="status-badge status-success">Active</span></td>
            </tr>
            <tr>
                <td>Auto-refresh</td>
                <td>Every 5 seconds</td>
            </tr>
        </table>
    </div>
    
    <div class="section">
        <h2>🚀 Quick Actions</h2>
        <div class="quick-actions">
            <a href="http://localhost:9079/api/progress" target="_blank" class="action-btn btn-primary">📊 Live Progress</a>
            <a href="http://localhost:9079/metrics" target="_blank" class="action-btn btn-success">📈 Metrics</a>
            <a href="http://localhost:9079/status" target="_blank" class="action-btn btn-warning">📋 Status</a>
            <a href="http://localhost:32400" target="_blank" class="action-btn btn-primary">🎬 Plex</a>
        </div>
    </div>
    
    <div class="section">
        <h2>📥 Download Performance by Tier</h2>
        <table>
            <tr>
                <th>Service/Tier</th>
                <th>Success ✓</th>
                <th>Failed ✗</th>
                <th>Total</th>
                <th>Success Rate</th>
            </tr>
            {tier_html}
        </table>
    </div>
    
    <div class="log-panel">
        <h3>📋 Recent Download Activity</h3>
        <table>
            <tr>
                <th>Method</th>
                <th>Status</th>
                <th>Time</th>
            </tr>
            {recent_html or "<tr><td colspan='3' style='text-align: center; color: #7f8c8d;'>No recent downloads</td></tr>"}
        </table>
    </div>
    
    <div class="section">
        <h2>📖 Quick Links</h2>
        <ul style="list-style: none; padding: 0;">
            <li style="padding: 10px 0; border-bottom: 1px solid #ecf0f1;">
                <a href="http://localhost:32400" target="_blank" style="color: #3498db; text-decoration: none; font-weight: 600;">🎬 Plex Media Server</a>
            </li>
            <li style="padding: 10px 0; border-bottom: 1px solid #ecf0f1;">
                <a href="http://localhost:9078" target="_blank" style="color: #3498db; text-decoration: none; font-weight: 600;">📊 Multi-Scrobbler</a>
            </li>
            <li style="padding: 10px 0;">
                <a href="/docs" style="color: #3498db; text-decoration: none; font-weight: 600;">📚 API Documentation</a>
            </li>
        </ul>
    </div>
    
    <script>
        // Add smooth scrolling and enhance interactivity
        document.querySelectorAll('.action-btn').forEach(btn => {{
            btn.addEventListener('mouseenter', function() {{
                this.style.transform = 'translateY(-2px)';
                this.style.boxShadow = '0 4px 12px rgba(0,0,0,0.2)';
            }});
            btn.addEventListener('mouseleave', function() {{
                this.style.transform = 'translateY(0)';
                this.style.boxShadow = 'none';
            }});
        }});
        
        // Add timestamp update
        setInterval(function() {{
            const now = new Date();
            console.log('Dashboard refreshed:', now.toLocaleTimeString());
        }}, 5000);
    </script>
</body>
</html>
    """
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
        import os
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



@app.get("/docs")
def docs():
    """
    GET /docs
    Returns API documentation for all endpoints.
    """
    docs_html = '''<!DOCTYPE html>\n<html>\n<head>\n    <title>Musicstream API Documentation</title>\n    <style>\n        body { font-family: 'Segoe UI', sans-serif; max-width: 900px; margin: 40px auto; padding: 20px; background: #f5f7fa; }\n        h1 { color: #2c3e50; border-bottom: 3px solid #3498db; padding-bottom: 10px; }\n        h2 { color: #34495e; border-bottom: 1px solid #ddd; padding-bottom: 5px; margin-top: 30px; }\n        .endpoint { background: white; padding: 20px; border-radius: 8px; margin: 20px 0; border-left: 4px solid #3498db; }\n        .endpoint h3 { margin: 0 0 10px 0; color: #2196F3; }\n        .get { background: #27ae60; color: white; }\n        .post { background: #e74c3c; color: white; }\n    </style>\n</head>\n<body>\n    <h1>Musicstream API Documentation</h1>\n    <p>Base URL: <strong>http://localhost:9079</strong></p>\n    <div class="endpoint">\n        <h3><span class="get">GET</span> /</h3>\n        <p>Interactive web dashboard</p>\n    </div>\n    <div class="endpoint">\n        <h3><span class="get">GET</span> /api/progress</h3>\n        <p>Real-time download progress</p>\n    </div>\n    <div class="endpoint">\n        <h3><span class="get">GET</span> /api/coverage</h3>\n        <p>Disk vs database completeness</p>\n    </div>\n    <div class="endpoint">\n        <h3><span class="get">GET</span> /api/report</h3>\n        <p>Missing and failed tracks report</p>\n    </div>\n    <div class="endpoint">\n        <h3><span class="get">GET</span> /health, /status, /metrics</h3>\n        <p>Health checks and metrics</p>\n    </div>\n    <h2>Control Endpoints (requires authentication if DAEMON_API_TOKEN is set)</h2>\n    <div class="endpoint">\n        <h3><span class="post">POST</span> /sync</h3>\n        <p>Trigger Spotify sync + download</p>\n    </div>\n    <div class="endpoint">\n        <h3><span class="post">POST</span> /integrity</h3>\n        <p>Run integrity check</p>\n    </div>\n    <div class="endpoint">\n        <h3><span class="post">POST</span> /discover</h3>\n        <p>Fetch ListenBrainz recommendations</p>\n    </div>\n    <div class="endpoint">\n        <h3><span class="post">POST</span> /admin/reset-failed</h3>\n        <p>Reset all failed tracks to pending status for retry</p>\n    </div>\n</body>\n</html>'''
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
