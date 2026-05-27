import logging
import os
import subprocess
from datetime import datetime, timezone
from typing import Optional

from src.core.config import LOG_DIR, BACKUP_DIR, MAX_BACKUPS, DISABLE_DOWNLOADS, SPOTIFY_CLIENT_ID

logger = logging.getLogger("musicstream.daemon")

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
    logger.info("Running Spotify incremental sync.")
    try:
        from src.db import get_session
        from src.ingestion.scraper import SpotifyScraper
        scraper = SpotifyScraper(client_id=SPOTIFY_CLIENT_ID)
        with get_session() as session:
            new_tracks = scraper.incremental_sync(session)
        logger.info("Spotify incremental sync complete: %d new tracks", new_tracks)
    except Exception as exc:
        logger.error("Spotify incremental sync failed: %s", exc, exc_info=True)


def spotify_saved_albums_sync() -> None:
    """Pull every Spotify Saved Album and upsert all its tracks."""
    logger.info("Running Spotify saved-albums sync.")
    try:
        from src.db import get_session
        from src.ingestion.scraper import SpotifyScraper
        scraper = SpotifyScraper(client_id=SPOTIFY_CLIENT_ID)
        with get_session() as session:
            new_tracks = scraper.saved_albums_sync(session)
        logger.info("Spotify saved-albums sync complete: %d new tracks", new_tracks)
    except Exception as exc:
        logger.error("Spotify saved-albums sync failed: %s", exc, exc_info=True)


def spotify_followed_artists_sync() -> None:
    """Pull every followed artist's full discography (heavy weekly sweep)."""
    logger.info("Running Spotify followed-artists sync.")
    try:
        from src.db import get_session
        from src.ingestion.scraper import SpotifyScraper
        scraper = SpotifyScraper(client_id=SPOTIFY_CLIENT_ID)
        with get_session() as session:
            new_tracks = scraper.followed_artists_sync(session)
        logger.info("Spotify followed-artists sync complete: %d new tracks", new_tracks)
    except Exception as exc:
        logger.error("Spotify followed-artists sync failed: %s", exc, exc_info=True)


def spotify_liked_artists_expand(batch_size: int = 50) -> None:
    """LIKED_ARTISTS_EXPAND_V1: discography-expand artists from Liked Songs +
    Saved Albums, including appears_on (guest features). Bounded daily job —
    chips away at the long tail without bursting the Spotify rate limiter."""
    logger.info("Running Spotify liked-artists expand (batch=%d).", batch_size)
    try:
        from src.db import get_session
        from src.ingestion.scraper import SpotifyScraper
        scraper = SpotifyScraper(client_id=SPOTIFY_CLIENT_ID)
        with get_session() as session:
            result = scraper.liked_artists_expand(session, batch_size=batch_size)
        logger.info(
            "Spotify liked-artists expand complete: %d artists, %d new tracks, %d remaining",
            result.get("artists_expanded", 0),
            result.get("new_tracks", 0),
            result.get("remaining", 0),
        )
    except Exception as exc:
        logger.error("Spotify liked-artists expand failed: %s", exc, exc_info=True)


def maybe_run_full_backfill() -> int:
    """One-time catch-up: if the DB has no album/artist sources yet, run a full
    backfill so saved-albums and followed-artists data lands. Idempotent — once
    the sources exist, this is a no-op on every subsequent boot.

    Returns the number of new tracks ingested (0 if skipped).
    """
    try:
        from src.db import get_session
        from src.models import Source, SourceType
        from src.ingestion.scraper import SpotifyScraper

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
        scraper = SpotifyScraper(client_id=SPOTIFY_CLIENT_ID)
        with get_session() as session:
            new_tracks = scraper.full_backfill(session)
        logger.info("Full backfill complete: %d new tracks", new_tracks)
        return new_tracks
    except Exception as exc:
        logger.error("Full backfill failed (non-fatal): %s", exc, exc_info=True)
        return 0

def download_pipeline() -> tuple[int, int]:
    """Run the download pipeline for all pending tracks. Returns (downloaded, failed)."""
    logger.info("Running download pipeline…")
    try:
        from src.db import get_session
        from src.ingestion.downloader import DownloadOrchestrator
        orchestrator = DownloadOrchestrator()

        # Phase 1: librespot
        try:
            with get_session() as session:
                lib_dl, lib_fail = orchestrator.download_pending_librespot(session)
            logger.info("librespot sweep: downloaded=%d failed=%d", lib_dl, lib_fail)
        except Exception as exc:
            logger.error("librespot sweep failed (non-fatal): %s", exc, exc_info=True)
            lib_dl = 0

        # Phase 2: Parallel batch
        with get_session() as session:
            downloaded, failed = orchestrator.download_pending(session)
        logger.info("Download pipeline complete: downloaded=%d failed=%d", downloaded, failed)
        downloaded += lib_dl

        # Phase 3: spotdl sweep
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

        # Sync Plex playlist for current month
        now = datetime.now(timezone.utc)
        month_name = now.strftime("%B")
        year = now.year
        plex_sync = PlexPlaylistSync()
        with get_session() as session:
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
    from src.ingestion.scraper import SpotifyScraper

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
    _record_run_start("scheduled")
    try:
        downloaded, failed = download_pipeline()
        _record_run_complete(downloaded=downloaded, failed=failed)
    except Exception as exc:
        logger.error("Full download pipeline error: %s", exc, exc_info=True)

def full_integrity_check() -> None:
    integrity_check()

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
    except Exception as exc:
        logger.warning("Could not record run completion: %s", exc)
