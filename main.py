from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
from importlib import import_module
from types import SimpleNamespace
from typing import List

from dotenv import load_dotenv  # type: ignore[import-untyped]

from exceptions import RateLimitError
from models import Track, TrackStatus
from ui import (
    console,
    print_error,
    print_fresh_start,
    print_header,
    print_integrity_result,
    print_interrupted,
    print_sources_table,
    print_success,
    print_summary,
    print_warning,
)

logger = logging.getLogger(__name__)

_MAX_DOWNLOAD_WORKERS = 4

# Updated validation targets to reflect the new module structure
_VALIDATION_TARGETS = [
    "main.py",
    "models.py",
    "db.py",
    "exceptions.py",
    "rate_limiter.py",
    "ui.py",
    "daemon.py",
    "ingestion/scraper.py",
    "ingestion/downloader.py",
    "ingestion/tagger.py",
    "ingestion/organiser.py",
    "discovery/listenbrainz.py",
    "discovery/plex_playlists.py",
    "integrity/checker.py",
]


# ── Logging bootstrap ─────────────────────────────────────────────────────────

def _configure_logging() -> None:
    log_dir = "logs"
    os.makedirs(log_dir, exist_ok=True)

    fmt = "%(asctime)s  %(levelname)-8s  %(name)s  %(message)s"
    rich_logging = import_module("rich.logging")
    rich_handler: logging.Handler = rich_logging.RichHandler(
        console=console,
        show_path=False,
        show_time=False,
        markup=True,
    )
    file_handler = logging.FileHandler(
        os.path.join(log_dir, "music-download-code.log"), encoding="utf-8"
    )
    file_handler.setFormatter(logging.Formatter(fmt))

    handlers: List[logging.Handler] = [rich_handler, file_handler]
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)


def _check_ffmpeg() -> bool:
    return shutil.which("ffmpeg") is not None or os.path.isfile("ffmpeg.exe")


def _require_client_id() -> str:
    client_id_raw = os.environ.get("SPOTIFY_CLIENT_ID", "")
    client_id = client_id_raw.strip().strip('"').strip("'")

    if not client_id:
        logger.error(
            "SPOTIFY_CLIENT_ID not found. Set it in your .env file.\n"
            "Get it at: https://developer.spotify.com/dashboard/applications"
        )
        sys.exit(1)

    invalid_placeholders = {
        "your_client_id_here",
        "replace_me",
        "changeme",
        "example",
        "abc123def456",
    }
    if client_id.lower() in invalid_placeholders:
        logger.error(
            "SPOTIFY_CLIENT_ID is still a placeholder value.\n"
            "Open .env and paste your real Client ID from the Spotify Developer Dashboard."
        )
        sys.exit(1)

    if re.fullmatch(r"[A-Za-z0-9]{32}", client_id) is None:
        recovered_candidates = re.findall(r"[A-Za-z0-9]{32}", client_id)
        if len(recovered_candidates) == 1:
            recovered_client_id = recovered_candidates[0]
            logger.warning("Recovered malformed SPOTIFY_CLIENT_ID value from .env")
            print_warning("Recovered malformed SPOTIFY_CLIENT_ID from .env. Please save the corrected value.")
            return recovered_client_id
        logger.error(
            "SPOTIFY_CLIENT_ID format looks invalid.\n"
            "Expected a 32-character alphanumeric value (no quotes, no spaces)."
        )
        sys.exit(1)

    return client_id


# ── Scrape command ─────────────────────────────────────────────────────────────

def cmd_scrape(args: argparse.Namespace) -> None:
    from db import get_session, init_db
    from ingestion.scraper import SpotifyScraper

    print_header("Scrape")
    client_id = _require_client_id()
    init_db()

    try:
        with get_session() as session:
            scraper = SpotifyScraper(client_id)

            if args.fresh:
                # Reset all non-downloaded tracks to pending
                reset = (
                    session.query(Track)
                    .filter(Track.status != TrackStatus.DOWNLOADED.value)
                    .update({"status": TrackStatus.PENDING.value})
                )
                print_fresh_start("scrape")
                if reset:
                    print_warning(f"Reset {reset} tracks to pending")

            new_tracks = scraper.full_backfill(session)

            counts = _get_track_counts(session)
            logger.info(
                "Scrape complete — new=%d pending=%d downloaded=%d failed=%d",
                new_tracks,
                counts.get("pending", 0),
                counts.get("downloaded", 0),
                counts.get("failed", 0),
            )
            print_success(f"Spotify discovery finished. {new_tracks} new tracks added.")
            print_summary(counts)

    except KeyboardInterrupt:
        print_interrupted("scrape", 0, 0)
    except RateLimitError as exc:
        print_error(str(exc))
        print_error("Process terminated due to rate limiting. Try again later or add cookies.txt")
        sys.exit(1)
    except Exception:
        logger.exception("Scrape failed")
        print_error("Scrape failed. See logs/music-download-code.log for details.")


# ── Download command ───────────────────────────────────────────────────────────

def cmd_download(args: argparse.Namespace) -> None:
    from db import get_session, init_db
    from ingestion.downloader import DownloadOrchestrator

    print_header("Download")
    if not _check_ffmpeg():
        logger.error(
            "FFmpeg not found. Install it or place ffmpeg.exe in the project directory.\n"
            "Download: https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
        )
        print_error("FFmpeg not found.")
        sys.exit(1)

    init_db()
    orchestrator = DownloadOrchestrator()

    try:
        with get_session() as session:
            if args.fresh:
                # Reset non-downloaded tracks to pending
                session.query(Track).filter(
                    Track.status.in_([
                        TrackStatus.DOWNLOADING.value,
                        TrackStatus.FAILED.value,
                    ])
                ).update({"status": TrackStatus.PENDING.value})
                print_fresh_start("download")

            pending_count = (
                session.query(Track)
                .filter(Track.status == TrackStatus.PENDING.value)
                .count()
            )

            if pending_count == 0:
                print_success("Nothing to download. Run 'scrape' first.")
                return

            logger.info("Downloading %d pending tracks", pending_count)
            # download_pending opens its own per-thread sessions via get_session()
            downloaded, failed = orchestrator.download_pending(session)

        logger.info("Download complete — %d downloaded, %d failed", downloaded, failed)
        print_summary({"downloaded": downloaded, "failed": failed, "total": downloaded + failed})

    except KeyboardInterrupt:
        print_interrupted("download", 0, 0)
    except RateLimitError as exc:
        print_error(str(exc))
        print_error("Process terminated due to rate limiting. Try again later or add cookies.txt")
        sys.exit(1)
    except Exception:
        logger.exception("Download failed")
        print_error("Download failed. See logs/music-download-code.log for details.")


# ── Status command ─────────────────────────────────────────────────────────────

def cmd_status(_args: argparse.Namespace) -> None:
    from db import get_session, init_db
    from models import Source

    init_db()
    try:
        with get_session() as session:
            print_header("musicstream Status")
            counts = _get_track_counts(session)
            print_summary(counts)

            sources = session.query(Source).all()
            if sources:
                encoding = sys.stdout.encoding or "utf-8"
                safe_sources = [
                    SimpleNamespace(
                        name=s.name.encode(encoding, errors="replace").decode(
                            encoding, errors="replace"
                        ),
                        source_type=SimpleNamespace(value=s.source_type),
                        last_scraped_at=s.last_scraped_at,
                    )
                    for s in sources
                ]
                print_sources_table(safe_sources)
    except Exception:
        logger.exception("Status failed")
        print_error("Status failed. See logs/music-download-code.log for details.")


def _get_track_counts(session) -> dict:
    """Return a dict of status → count for all tracks."""
    from sqlalchemy import func
    rows = (
        session.query(Track.status, func.count(Track.id))
        .group_by(Track.status)
        .all()
    )
    counts = {status: count for status, count in rows}
    counts["total"] = sum(counts.values())
    return counts


# ── Integrity command ──────────────────────────────────────────────────────────

def cmd_integrity(_args: argparse.Namespace) -> None:
    from db import get_session, init_db
    from integrity.checker import IntegrityChecker

    print_header("Integrity Check")
    init_db()

    try:
        checker = IntegrityChecker()
        with get_session() as session:
            result = checker.run(session)
        print_integrity_result(result)
        print_success(
            f"Integrity check complete: {result.total_checked} checked, "
            f"{result.ok} ok, {result.missing} missing, {result.corrupt} corrupt"
        )
    except Exception:
        logger.exception("Integrity check failed")
        print_error("Integrity check failed. See logs/music-download-code.log for details.")


# ── Daemon command ─────────────────────────────────────────────────────────────

def cmd_daemon(_args: argparse.Namespace) -> None:
    """Start the musicstream daemon (APScheduler + Flask control plane)."""
    print_header("Daemon")
    try:
        import daemon as daemon_module
        daemon_module.startup_sequence()
        daemon_module.app.run(host="0.0.0.0", port=9079, threaded=True)
    except Exception:
        logger.exception("Daemon failed to start")
        print_error("Daemon failed. See logs/daemon.log for details.")
        sys.exit(1)


# ── Validate command ───────────────────────────────────────────────────────────

def cmd_validate(_args: argparse.Namespace) -> None:
    print_header("Project Validation")

    # Only validate files that actually exist
    existing_targets = [t for t in _VALIDATION_TARGETS if os.path.exists(t)]

    steps: list[tuple[str, list[str]]] = [
        ("Ruff lint", [sys.executable, "-m", "ruff", "check", *existing_targets]),
        (
            "Mypy type check",
            [
                sys.executable,
                "-m",
                "mypy",
                "--disable-error-code=call-overload",
                "--disable-error-code=method-assign",
                *existing_targets,
            ],
        ),
    ]
    for label, command in steps:
        print_success(f"Running {label}...")
        result = subprocess.run(command, check=False)
        if result.returncode != 0:
            print_error(f"{label} failed")
            sys.exit(result.returncode)
        print_success(f"{label} passed")
    print_success("Project validation passed")


# ── CLI ────────────────────────────────────────────────────────────────────────

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="musicstream",
        description="Self-hosted autonomous music pipeline",
    )
    sub = parser.add_subparsers(dest="command")

    # scrape
    scrape_parser = sub.add_parser(
        "scrape", help="Discover Spotify playlists + Liked Songs and ingest into DB"
    )
    scrape_parser.add_argument(
        "--fresh", action="store_true", help="Reset non-downloaded tracks and start from scratch"
    )

    # download
    dl = sub.add_parser("download", help="Download all pending tracks via 5-tier chain")
    dl.add_argument(
        "--fresh", action="store_true", help="Reset failed/downloading tracks and retry all"
    )

    # status
    sub.add_parser("status", help="Show current database status and source list")

    # integrity
    sub.add_parser("integrity", help="Run file integrity check (missing/corrupt files)")

    # daemon
    sub.add_parser(
        "daemon",
        help="Start the long-running daemon (APScheduler + Flask control plane on :9079)",
    )

    # validate
    sub.add_parser("validate", help="Run project lint and type checks")

    return parser


def main() -> None:
    _configure_logging()
    load_dotenv()

    parser = build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    commands = {
        "scrape":    cmd_scrape,
        "download":  cmd_download,
        "status":    cmd_status,
        "integrity": cmd_integrity,
        "daemon":    cmd_daemon,
        "validate":  cmd_validate,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
