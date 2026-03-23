from __future__ import annotations

import argparse
import logging
import os
import re
import shutil
import subprocess
import sys
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from importlib import import_module
from types import SimpleNamespace
from typing import TYPE_CHECKING, List, cast

from dotenv import load_dotenv  # type: ignore[import-untyped]

from exceptions import RateLimitError
from models import DatabaseManager, SourceType, Track, TrackStatus
from ui import (
    confirm_resume,
    console,
    create_progress,
    print_error,
    print_fresh_start,
    print_header,
    print_interrupted,
    print_sources_table,
    print_success,
    print_summary,
    print_warning,
)

if TYPE_CHECKING:
    from spotify_client import SpotifyIngestor
    from ytm_client import YTMResolver

logger = logging.getLogger(__name__)

_MAX_DOWNLOAD_WORKERS = 4
_VALIDATION_TARGETS = [
    "main.py",
    "downloader.py",
    "models.py",
    "spotify_client.py",
    "ytm_client.py",
    "robustness.py",
    "exceptions.py",
    "ui.py",
    "test_robustness.py",
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
    from spotify_client import SpotifyIngestor

    print_header("Scrape")
    client_id = _require_client_id()
    db = DatabaseManager()
    spotify = SpotifyIngestor(client_id)

    try:
        if args.fresh:
            reset = db.reset_tracks_for_fresh_scrape()
            print_fresh_start("scrape")
            if reset:
                print_warning(f"Reset {reset} tracks to pending")

        new_tracks = _scrape_all_sources(db, spotify)

        counts = db.get_track_counts()
        logger.info(
            "Scrape complete — new=%d pending=%d resolved=%d failed=%d downloaded=%d",
            new_tracks,
            counts.get("pending", 0),
            counts.get("resolved", 0),
            counts.get("failed", 0),
            counts.get("downloaded", 0),
        )
        print_success("Spotify discovery finished. No YouTube resolution was run.")
        print_summary(counts)
    except KeyboardInterrupt:
        counts = db.get_track_counts()
        print_interrupted("scrape", counts.get("total", 0) - counts.get("pending", 0), counts.get("total", 0))
    except RateLimitError as exc:
        print_error(str(exc))
        print_error("Process terminated due to rate limiting. Try again later or add cookies.txt")
        sys.exit(1)
    except Exception:
        logger.exception("Scrape failed")
        print_error("Scrape failed. See logs/music-download-code.log for details.")
    finally:
        db.close()


def _scrape_all_sources(db: DatabaseManager, spotify: SpotifyIngestor) -> int:
    """Returns total new tracks added."""
    logger.info("Discovering playlists...")
    playlists = spotify.get_all_playlists()
    logger.info("Found %d playlists", len(playlists))

    total_new = 0
    progress = create_progress()
    with progress:
        task = progress.add_task("Scraping playlists", total=len(playlists))
        for pl in playlists:
            progress.update(task, description=f"Scraping: {pl['name'][:40]}")
            db.upsert_source(pl["spotify_id"], pl["name"], SourceType.PLAYLIST)
            tracks = spotify.get_playlist_tracks(pl["spotify_id"])
            new_count = _ingest_tracks(db, tracks, pl["spotify_id"])
            db.mark_source_scraped(pl["spotify_id"])
            total_new += new_count
            logger.info("Scraped %s: %d new tracks", pl["name"], new_count)
            progress.advance(task)

        liked_task = progress.add_task("Scraping Liked Songs", total=1)
        liked_source_id = "__liked_songs__"
        db.upsert_source(liked_source_id, "Liked Songs", SourceType.LIKED)
        liked = spotify.get_liked_songs()
        new_count = _ingest_tracks(db, liked, liked_source_id)
        db.mark_source_scraped(liked_source_id)
        total_new += new_count
        logger.info("Liked Songs: %d new tracks", new_count)
        progress.advance(liked_task)

    return total_new


def _ingest_tracks(
    db: DatabaseManager, tracks: List[dict], source_spotify_id: str
) -> int:
    new_count = 0
    for t in tracks:
        existing = db.get_track_by_spotify_uri(t["spotify_uri"])
        if not existing:
            new_count += 1
        db.add_track(
            spotify_uri=t["spotify_uri"],
            track_name=t["track_name"],
            artist_name=t["artist_name"],
            album_name=t.get("album_name"),
            track_number=t.get("track_number"),
            duration_ms=t.get("duration_ms"),
            source_spotify_id=source_spotify_id,
        )
    return new_count


def _resolve_pending(db: DatabaseManager, ytm: YTMResolver) -> tuple[int, int]:
    """Returns (resolved_count, failed_count)."""
    pending = db.get_pending_tracks()
    if not pending:
        print_success("No pending tracks to resolve")
        return 0, 0

    resolved = 0
    failed = 0
    progress = create_progress()
    with progress:
        task = progress.add_task("Resolving on YouTube Music", total=len(pending))
        for track in pending:
            progress.update(
                task,
                description=(
                    f"Resolving: {track.track_name[:35]} - {track.artist_name[:20]}"
                ),
            )
            video_id = ytm.search_track(
                track.track_name, track.artist_name, duration_ms=track.duration_ms
            )
            if video_id:
                db.update_track_video_id(track.spotify_uri, video_id)
                resolved += 1
            else:
                db.update_track_status(track.spotify_uri, TrackStatus.FAILED)
                failed += 1
            progress.advance(task)

    logger.info("Resolution: %d resolved, %d failed", resolved, failed)
    return resolved, failed


# ── Download command ───────────────────────────────────────────────────────────

def cmd_download(args: argparse.Namespace) -> None:
    from downloader import AudioExtractor

    print_header("Download")
    if not _check_ffmpeg():
        logger.error(
            "FFmpeg not found. Install it or place ffmpeg.exe in the project directory.\n"
            "Download: https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip"
        )
        print_error("FFmpeg not found.")
        sys.exit(1)

    download_dir = args.output
    if not download_dir:
        download_dir = input("Where should downloads be saved? (default: downloads): ").strip()
        if not download_dir:
            download_dir = "downloads"

    db = DatabaseManager()
    downloader = AudioExtractor(
        download_dir,
        chaos_enabled=args.chaos,
        chaos_intensity=args.chaos_intensity,
    )

    try:
        interrupted_count = db.get_interrupted_download_count()
        if args.fresh:
            if interrupted_count:
                db.reset_interrupted_downloads()
            print_fresh_start("download")
        elif interrupted_count:
            total_resolved = len(db.get_tracks_by_status(TrackStatus.RESOLVED))
            confirm_resume("download", total_resolved, total_resolved + interrupted_count)
            db.reset_interrupted_downloads()
            print_warning(f"Recovered {interrupted_count} interrupted downloads")

        resolved = db.get_tracks_by_status(TrackStatus.RESOLVED)
        logger.info("Downloading %d resolved tracks to %s", len(resolved), download_dir)

        if not resolved:
            print_success("Nothing to download. Run 'scrape' first.")
            return

        downloaded = 0
        failed = 0
        interrupted = False

        progress = create_progress()
        with progress:
            task = progress.add_task(
                "Downloading", total=len([t for t in resolved if t.yt_video_id])
            )

            def _download_one(track: Track) -> tuple[str, bool, str | None]:
                db.update_track_status(track.spotify_uri, TrackStatus.DOWNLOADING)
                result = downloader.extract_audio(
                    track.yt_video_id or "",
                    track.track_name,
                    track.artist_name,
                    album_name=track.album_name or "",
                    force=args.fresh,
                )
                return track.spotify_uri, result is not None, result

            pool: ThreadPoolExecutor | None = None
            try:
                with ThreadPoolExecutor(max_workers=_MAX_DOWNLOAD_WORKERS) as pool:
                    futures = {
                        pool.submit(_download_one, t): t
                        for t in resolved
                        if t.yt_video_id
                    }
                    for future in as_completed(futures):
                        track = futures[future]
                        try:
                            uri, success, filepath = future.result()
                            if success:
                                db.update_track_status(uri, TrackStatus.DOWNLOADED)
                                logger.info("Downloaded: %s", filepath)
                                downloaded += 1
                            else:
                                db.update_track_status(uri, TrackStatus.FAILED)
                                logger.warning(
                                    "Failed: %s by %s", track.track_name, track.artist_name
                                )
                                failed += 1
                        except Exception:
                            logger.exception("Error downloading %s", track.track_name)
                            db.update_track_status(track.spotify_uri, TrackStatus.FAILED)
                            failed += 1
                        progress.advance(task)
            except KeyboardInterrupt:
                interrupted = True
                if pool is not None:
                    pool.shutdown(wait=False, cancel_futures=True)

        if interrupted:
            reset_count = db.reset_interrupted_downloads()
            downloader.cleanup_partial_files()
            print_interrupted("download", downloaded, downloaded + failed + reset_count)
            return

        downloader.cleanup_partial_files()
        logger.info("Download complete — %d downloaded, %d failed", downloaded, failed)
        print_summary(
            {
                "downloaded": downloaded,
                "failed": failed,
                "total": downloaded + failed,
            }
        )

    except KeyboardInterrupt:
        print_interrupted("download", 0, 0)
    except RateLimitError as exc:
        print_error(str(exc))
        print_error("Process terminated due to rate limiting. Try again later or add cookies.txt")
        sys.exit(1)
    except Exception:
        logger.exception("Download failed")
        print_error("Download failed. See logs/music-download-code.log for details.")
    finally:
        db.close()


# ── Status command ─────────────────────────────────────────────────────────────

def cmd_status(_args: argparse.Namespace) -> None:
    db = DatabaseManager()
    try:
        print_header("music-download-code Status")
        counts = db.get_track_counts()
        print_summary(counts)

        sources = db.get_all_sources()
        if sources:
            encoding = sys.stdout.encoding or "utf-8"
            safe_sources = [
                SimpleNamespace(
                    name=s.name.encode(encoding, errors="replace").decode(
                        encoding, errors="replace"
                    ),
                    source_type=s.source_type,
                    last_scraped_at=s.last_scraped_at,
                )
                for s in sources
            ]
            print_sources_table(safe_sources)
    finally:
        db.close()


# ── Resolve command ────────────────────────────────────────────────────────────

def cmd_resolve(args: argparse.Namespace) -> None:
    from ytm_client import YTMResolver

    print_header("Resolve")
    db = DatabaseManager()
    ytm = YTMResolver(
        chaos_enabled=args.chaos,
        chaos_intensity=args.chaos_intensity,
    )

    try:
        if args.fresh:
            reset = db.reset_tracks_for_fresh_scrape()
            print_fresh_start("resolve")
            if reset:
                print_warning(f"Reset {reset} tracks to pending")

        if not args.skip_health_check:
            print_success("Running resolver health check before long resolve...")
            health = ytm.health_check()
            filter_summary = ", ".join(
                f"{item['filter']}={item['count']}" for item in health["filters"]
            )
            if health["ok"]:
                if health["ytm_ok"]:
                    print_success(f"Resolver health check passed (ytmusicapi results: {filter_summary})")
                else:
                    print_warning(
                        "Resolver health check: ytmusicapi is degraded, yt-dlp fallback is available"
                    )
            else:
                print_error("Resolver health check failed before resolve start.")
                print_error("No results from ytmusicapi filters or yt-dlp fallback.")
                print_error("Add headers_auth.json and verify internet/cookies, then retry.")
                sys.exit(1)

        resolved_count, failed_count = _resolve_pending(db, ytm)

        counts = db.get_track_counts()
        logger.info(
            "Resolve complete — pending=%d resolved=%d failed=%d",
            counts.get("pending", 0),
            counts.get("resolved", 0),
            counts.get("failed", 0),
        )
        logger.info("Resolved this run: %d, Failed this run: %d", resolved_count, failed_count)
        print_summary(counts)
    except KeyboardInterrupt:
        counts = db.get_track_counts()
        completed_count = (
            counts.get("resolved", 0)
            + counts.get("failed", 0)
            + counts.get("downloaded", 0)
        )
        total_count = counts.get("total", 0)
        print_interrupted("resolve", completed_count, total_count)
    except RateLimitError as exc:
        print_error(str(exc))
        print_error("Process terminated due to rate limiting. Try again later or add cookies.txt")
        sys.exit(1)
    except Exception:
        logger.exception("Resolve failed")
        print_error("Resolve failed. See logs/music-download-code.log for details.")
    finally:
        db.close()


# ── Retry command ──────────────────────────────────────────────────────────────

def cmd_retry(args: argparse.Namespace) -> None:
    from downloader import AudioExtractor

    print_header("Retry")
    if not _check_ffmpeg():
        logger.error("FFmpeg not found.")
        print_error("FFmpeg not found.")
        sys.exit(1)

    download_dir = args.output or "downloads"
    db = DatabaseManager()
    downloader = AudioExtractor(
        download_dir,
        chaos_enabled=args.chaos,
        chaos_intensity=args.chaos_intensity,
    )
    recovered = 0
    failed: list[Track] = []

    try:
        if args.fresh:
            failed = list(db.get_tracks_by_status(TrackStatus.FAILED)) + list(
                db.get_tracks_by_status(TrackStatus.FAILED_VALIDATION)
            )
            print_fresh_start("retry")
        else:
            failed = list(db.get_tracks_by_status(TrackStatus.FAILED))

        logger.info("Retrying %d failed tracks", len(failed))

        if not failed:
            print_success("No failed tracks to retry")
            return

        still_failed = 0
        progress = create_progress()
        with progress:
            task = progress.add_task("Retrying failed", total=len(failed))
            for track in failed:
                if not track.yt_video_id:
                    still_failed += 1
                    progress.advance(task)
                    continue

                progress.update(task, description=f"Retrying: {track.track_name[:35]}")
                result = downloader.extract_audio(
                    track.yt_video_id,
                    track.track_name,
                    track.artist_name,
                    album_name=track.album_name or "",
                    force=args.fresh,
                )
                if result:
                    db.update_track_status(track.spotify_uri, TrackStatus.DOWNLOADED)
                    logger.info("Recovered: %s", result)
                    recovered += 1
                else:
                    logger.warning("Still failed: %s by %s", track.track_name, track.artist_name)
                    still_failed += 1
                progress.advance(task)

        logger.info("Retry complete: %d recovered out of %d", recovered, len(failed))
        print_summary(
            {"recovered": recovered, "still_failed": still_failed, "total": len(failed)}
        )
    except KeyboardInterrupt:
        print_interrupted("retry", recovered if 'recovered' in locals() else 0, len(failed) if 'failed' in locals() else 0)
    except Exception:
        logger.exception("Retry failed")
        print_error("Retry failed. See logs/music-download-code.log for details.")
    finally:
        db.close()


# ── Chaos Test command ─────────────────────────────────────────────────────────

def cmd_chaos_test(args: argparse.Namespace) -> None:
    from downloader import AudioExtractor
    from robustness import MusicDownloadChaosMonkey
    from ytm_client import YTMResolver

    print_header("Chaos Test")
    monkey = MusicDownloadChaosMonkey(enabled=True, intensity=args.chaos_intensity)
    resolver = YTMResolver(chaos_enabled=True, chaos_intensity=args.chaos_intensity)
    downloader = AudioExtractor(
        args.output or "downloads",
        chaos_enabled=True,
        chaos_intensity=args.chaos_intensity,
    )

    def _run_operation(operation: str) -> None:
        if operation == "spotify_playlist_fetch":
            time.sleep(0.01)
            return
        if operation == "youtube_search":
            resolver.search_track("Get Lucky", "Daft Punk", 248000)
            return
        if operation == "download_track":
            downloader.cleanup_partial_files()
            return
        if operation == "database_update":
            db = DatabaseManager()
            try:
                db.get_track_counts()
            finally:
                db.close()

    results = monkey.run_chaos_test_suite(args.duration_seconds, _run_operation)
    total = int(results["total_operations"])
    failed = int(results["failed_operations"])
    recovered = int(results["recovered_operations"])
    failure_rate = (failed / total * 100) if total else 0.0
    recovery_rate = (recovered / failed * 100) if failed else 100.0
    failure_types = cast(dict[str, int], results["failure_types"])

    print_success(
        f"Chaos run complete: total={total} failed={failed} "
        f"({failure_rate:.1f}%) recovered={recovered} ({recovery_rate:.1f}%)"
    )
    if failure_types:
        summary = ", ".join(f"{k}={v}" for k, v in sorted(failure_types.items()))
        print_warning(f"Failure distribution: {summary}")


def cmd_validate(_args: argparse.Namespace) -> None:
    print_header("Project Validation")
    steps: list[tuple[str, list[str]]] = [
        ("Ruff lint", [sys.executable, "-m", "ruff", "check", *_VALIDATION_TARGETS]),
        (
            "Mypy type check",
            [
                sys.executable,
                "-m",
                "mypy",
                "--disable-error-code=call-overload",
                "--disable-error-code=method-assign",
                *_VALIDATION_TARGETS,
            ],
        ),
        ("Unit tests", [sys.executable, "-m", "unittest", "test_robustness.py"]),
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
        prog="music-download-code",
        description="Sync Spotify playlists to local audio files via YouTube Music",
    )
    sub = parser.add_subparsers(dest="command")

    scrape_parser = sub.add_parser(
        "scrape", help="Discover Spotify playlists + Liked Songs only"
    )
    scrape_parser.add_argument(
        "--fresh", action="store_true", help="Start from scratch, ignoring previous progress"
    )

    dl = sub.add_parser("download", help="Download all resolved tracks")
    dl.add_argument("-o", "--output", type=str, default="", help="Output directory")
    dl.add_argument(
        "--fresh", action="store_true", help="Start from scratch, ignoring previous progress"
    )
    dl.add_argument("--chaos", action="store_true", help="Enable chaos injection during command")
    dl.add_argument(
        "--chaos-intensity",
        type=str,
        choices=("low", "medium", "high"),
        default="low",
        help="Chaos injection intensity",
    )

    sub.add_parser("status", help="Show current database status")

    resolve_parser = sub.add_parser(
        "resolve", help="Only resolve existing pending tracks on YouTube Music (skip Spotify scraping)"
    )
    resolve_parser.add_argument(
        "--fresh", action="store_true", help="Start from scratch, ignoring previous progress"
    )
    resolve_parser.add_argument(
        "--skip-health-check",
        action="store_true",
        help="Skip resolver preflight health check",
    )
    resolve_parser.add_argument(
        "--chaos", action="store_true", help="Enable chaos injection during command"
    )
    resolve_parser.add_argument(
        "--chaos-intensity",
        type=str,
        choices=("low", "medium", "high"),
        default="low",
        help="Chaos injection intensity",
    )

    rt = sub.add_parser("retry", help="Retry failed downloads")
    rt.add_argument("-o", "--output", type=str, default="", help="Output directory")
    rt.add_argument(
        "--fresh", action="store_true", help="Include validation failures in retry"
    )
    rt.add_argument("--chaos", action="store_true", help="Enable chaos injection during command")
    rt.add_argument(
        "--chaos-intensity",
        type=str,
        choices=("low", "medium", "high"),
        default="low",
        help="Chaos injection intensity",
    )

    chaos = sub.add_parser("chaos-test", help="Run chaos robustness simulation suite")
    chaos.add_argument(
        "--duration-seconds",
        type=int,
        default=20,
        help="Duration in seconds for chaos simulation",
    )
    chaos.add_argument("-o", "--output", type=str, default="", help="Output directory")
    chaos.add_argument(
        "--chaos-intensity",
        type=str,
        choices=("low", "medium", "high"),
        default="medium",
        help="Chaos injection intensity",
    )

    sub.add_parser("validate", help="Run project lint, type checks, and tests")

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
        "scrape": cmd_scrape,
        "download": cmd_download,
        "status": cmd_status,
        "resolve": cmd_resolve,
        "retry": cmd_retry,
        "chaos-test": cmd_chaos_test,
        "validate": cmd_validate,
    }
    commands[args.command](args)


if __name__ == "__main__":
    main()
