"""Reconcile the ``tracks`` table with audio files already on disk.

Background
----------
On 2026-05-20 the postgres database was wiped and recreated, but the audio
files on the external HDD (~12,973 across 3,165 artist folders) were left
intact. The daemon has no startup reconcile pass, so it now treats every
existing file as missing and re-queues it as ``pending``.

Strategy
--------
Pass 1 — canonical-path match (cheap):
    For every track whose ``file_path`` is NULL, compute the canonical path
    that ``FileOrganiser._build_path()`` would have produced. If that path
    exists on disk, link the row and mark ``status='downloaded'``.

Pass 2 — orphan report (read-only):
    Walk the media root and list every audio file that was NOT linked by
    any track row in pass 1. These are candidates for follow-up (fuzzy
    matching, rename, etc.) — this script does not touch them.

What the script DOES NOT do
---------------------------
- Compute SHA-256 (too slow on USB; deferred).
- Read embedded tags (organiser does not embed spotify_id, so tags would
  not help for this corpus).
- Modify any file on disk.
- Touch tracks that already have ``file_path`` set.

Usage
-----
    # dry run (default): no DB writes, prints summary
    python -m src.scripts.reconcile_disk

    # apply linking
    python -m src.scripts.reconcile_disk --apply

    # custom media root (defaults to /media inside container, Y:\\music on host)
    python -m src.scripts.reconcile_disk --media-root /media --apply
"""

from __future__ import annotations

import argparse
import logging
import os
import re
import sys
from collections import Counter
from pathlib import Path
from typing import Iterable, Optional

from sqlalchemy import select

# Path bootstrapping when run as a stand-alone script
_HERE = Path(__file__).resolve()
_REPO_ROOT = _HERE.parents[2]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

from src.models import Track  # noqa: E402
from src.db import get_session, init_db  # noqa: E402

logger = logging.getLogger("reconcile_disk")

# ── Sanitisation rules (mirror src/ingestion/organiser.py exactly) ────────────
_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*]')
_WINDOWS_RESERVED = {
    "CON", "PRN", "AUX", "NUL",
    *(f"COM{i}" for i in range(1, 10)),
    *(f"LPT{i}" for i in range(1, 10)),
}
_AUDIO_EXTS = (".flac", ".mp3", ".m4a", ".ogg", ".opus")


def _sanitize(name: str) -> str:
    """Byte-for-byte copy of FileOrganiser._sanitize()."""
    s = _FORBIDDEN_RE.sub("_", name)
    s = s[:200]
    s = s.strip(". ")
    stem = s.split(".", 1)[0]
    if stem.upper() in _WINDOWS_RESERVED:
        s = f"_{stem}_" + s[len(stem):]
    return s


def _build_path(track: Track, media_root: str, fmt: Optional[str] = None) -> str:
    """Mirror of FileOrganiser._build_path() for one (track, format) pair."""
    fmt = fmt or track.format or "mp3"
    ext = f".{fmt}"

    album_artist = track.album_artist or track.artist or "Unknown Artist"
    artist_folder = _sanitize(album_artist)

    album = track.album or "Unknown Album"
    if track.year:
        album_folder = _sanitize(f"{album} ({track.year})")
    else:
        album_folder = _sanitize(album)

    title = track.title or "Unknown Title"
    if track.track_number is not None:
        nn = str(track.track_number).zfill(2)
        filename = _sanitize(f"{nn} - {title}") + ext
    else:
        filename = _sanitize(title) + ext

    return os.path.join(media_root, artist_folder, album_folder, filename)


def _candidate_paths(track: Track, media_root: str) -> list[tuple[str, str]]:
    """Build a list of (format, path) candidates to try, most-likely first.

    If track.format is set, try that extension first. Then try the others.
    """
    seen: set[str] = set()
    out: list[tuple[str, str]] = []
    formats = [track.format] if track.format else []
    formats.extend(["flac", "mp3", "m4a", "ogg", "opus"])
    for fmt in formats:
        if not fmt or fmt in seen:
            continue
        seen.add(fmt)
        out.append((fmt, _build_path(track, media_root, fmt)))
    return out


def _walk_audio(media_root: str) -> Iterable[str]:
    """Yield every audio file under media_root."""
    for root, _, files in os.walk(media_root):
        for f in files:
            if f.lower().endswith(_AUDIO_EXTS):
                yield os.path.join(root, f)


def _normalise_for_match(s: str) -> str:
    """Lowercase + strip non-alphanumerics for fuzzy comparison."""
    return re.sub(r"[^a-z0-9]+", "", s.lower())


def _build_disk_index(media_root: str) -> dict[tuple[str, str], list[tuple[str, str]]]:
    """Index on-disk audio files by (normalised_artist_folder, normalised_title_stem).

    For each file ``/media/<artist>/<album>/<NN - title>.<ext>`` we record
    ``(_normalise_for_match(artist), _normalise_for_match(title))`` ->
    [(format, full_path), ...]. The leading "NN - " track number prefix is
    stripped from the title. This is a best-effort fuzzy index for catching
    files whose album folder differs from what the DB row would compute.
    """
    index: dict[tuple[str, str], list[tuple[str, str]]] = {}
    nn_prefix = re.compile(r"^\d{1,3}\s*-\s*")
    count = 0
    for path in _walk_audio(media_root):
        rel = os.path.relpath(path, media_root)
        parts = rel.replace("\\", "/").split("/")
        if len(parts) < 2:
            continue  # not under <artist>/...
        artist_folder = parts[0]
        stem, ext = os.path.splitext(parts[-1])
        title = nn_prefix.sub("", stem)
        key = (_normalise_for_match(artist_folder), _normalise_for_match(title))
        index.setdefault(key, []).append((ext.lstrip(".").lower(), path))
        count += 1
        if count % 2000 == 0:
            logger.info("  indexed %d files", count)
    logger.info("Disk index: %d files, %d unique (artist,title) keys", count, len(index))
    return index


def reconcile(media_root: str, apply_changes: bool, limit: Optional[int],
              skip_orphan_scan: bool = False) -> int:
    """Run pass 1 (link) and (optionally) pass 2 (orphan report).

    Returns
    -------
    int
        Number of tracks linked in pass 1.
    """
    media_root = media_root.rstrip("/\\")

    if not os.path.isdir(media_root):
        logger.error("Media root not found: %s", media_root)
        return 0

    matched_paths: set[str] = set()
    linked_canonical = 0
    linked_fuzzy = 0
    no_match = 0
    fmt_counter: Counter[str] = Counter()

    # Build the disk index up front (only when we'll need it).
    disk_index: dict[tuple[str, str], list[tuple[str, str]]] = {}
    if not skip_orphan_scan:
        logger.info("Building disk index of audio files under %s ...", media_root)
        disk_index = _build_disk_index(media_root)

    with get_session() as session:
        # Only consider tracks that don't already have a file_path
        stmt = select(Track).where(Track.file_path.is_(None))
        if limit:
            stmt = stmt.limit(limit)

        tracks = list(session.scalars(stmt))
        logger.info("Inspecting %d tracks with file_path IS NULL", len(tracks))

        for i, track in enumerate(tracks, 1):
            if i % 1000 == 0:
                logger.info("  ... checked %d / %d", i, len(tracks))

            hit_path: Optional[str] = None
            hit_fmt: Optional[str] = None
            match_kind: str = "canonical"
            for fmt, candidate in _candidate_paths(track, media_root):
                try:
                    if os.path.isfile(candidate):
                        hit_path = candidate
                        hit_fmt = fmt
                        break
                except OSError:
                    continue

            # Fuzzy fallback: same artist folder + title (any album folder)
            if hit_path is None and disk_index:
                artist = track.album_artist or track.artist or ""
                title = track.title or ""
                # Try canonical-sanitised artist folder and the raw artist string
                key_candidates = {
                    (_normalise_for_match(_sanitize(artist)), _normalise_for_match(title)),
                    (_normalise_for_match(artist), _normalise_for_match(title)),
                }
                for key in key_candidates:
                    bucket = disk_index.get(key)
                    if not bucket:
                        continue
                    # Prefer flac > mp3 > others
                    bucket_sorted = sorted(
                        bucket,
                        key=lambda fp: {"flac": 0, "mp3": 1}.get(fp[0], 9),
                    )
                    hit_fmt, hit_path = bucket_sorted[0]
                    match_kind = "fuzzy"
                    break

            if hit_path is None:
                no_match += 1
                if no_match <= 10:
                    cands = _candidate_paths(track, media_root)
                    logger.info(
                        "  NO-MATCH track id=%d artist=%r album=%r title=%r year=%r tn=%s fmt=%s",
                        track.id, track.album_artist or track.artist, track.album,
                        track.title, track.year, track.track_number, track.format,
                    )
                    for fmt, p in cands[:2]:
                        logger.info("    candidate (%s): %s", fmt, p)
                continue

            try:
                size = os.path.getsize(hit_path)
            except OSError:
                size = None

            matched_paths.add(os.path.normcase(hit_path))
            fmt_counter[hit_fmt or "?"] += 1
            if match_kind == "canonical":
                linked_canonical += 1
            else:
                linked_fuzzy += 1

            if apply_changes:
                track.file_path = hit_path
                track.file_size_bytes = size
                track.format = hit_fmt
                track.status = "downloaded"
                # P1-2: record provenance so the downloaded-without-method
                # integrity invariant does not trip on disk-reconciled rows.
                if not track.download_method:
                    track.download_method = "disk_reconcile"
                # plex_verified left False so a future verify pass picks it up

        if apply_changes:
            session.commit()
            logger.info("Committed %d row updates", linked_canonical + linked_fuzzy)
        else:
            session.rollback()
            logger.info("DRY RUN — no DB changes committed")

    # ── Pass 2: orphan inventory ──────────────────────────────────────────────
    orphans: list[str] = []
    total_files = 0
    if disk_index:
        # Re-walk to count and identify orphans against matched_paths
        for bucket in disk_index.values():
            for _, p in bucket:
                total_files += 1
                if os.path.normcase(p) not in matched_paths:
                    orphans.append(p)

    logger.info("=" * 60)
    logger.info("RECONCILE SUMMARY")
    logger.info("=" * 60)
    logger.info("DB tracks with NULL file_path inspected : %d", len(tracks))
    logger.info("  -> linked via canonical path          : %d", linked_canonical)
    logger.info("  -> linked via fuzzy (artist,title)    : %d", linked_fuzzy)
    logger.info("  -> no match                           : %d", no_match)
    logger.info("Audio files on disk                     : %d", total_files)
    logger.info("  -> orphans (file with no DB row)      : %d", len(orphans))
    if fmt_counter:
        logger.info("Linked by format:")
        for fmt, n in fmt_counter.most_common():
            logger.info("  %-6s : %d", fmt, n)

    # write orphan list for follow-up
    if disk_index:
        orphan_log = os.path.join(_REPO_ROOT, "logs", "reconcile_orphans.txt")
        os.makedirs(os.path.dirname(orphan_log), exist_ok=True)
        with open(orphan_log, "w", encoding="utf-8") as fh:
            for p in sorted(orphans):
                fh.write(p + "\n")
        logger.info("Orphan list written to %s", orphan_log)

    return linked_canonical + linked_fuzzy


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--media-root",
        default=os.environ.get("MEDIA_DRIVE", "/media"),
        help="Media root directory (default: $MEDIA_DRIVE or /media)",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Commit DB changes. Without this flag the script is read-only.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Inspect only the first N tracks (for sampling).",
    )
    parser.add_argument(
        "--skip-orphan-scan",
        action="store_true",
        help="Skip the pass-2 walk over media root (faster on slow USB).",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )
    args = parser.parse_args()

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    init_db()
    reconcile(args.media_root, apply_changes=args.apply, limit=args.limit,
              skip_orphan_scan=args.skip_orphan_scan)


if __name__ == "__main__":
    main()
