#!/usr/bin/env python3
"""
Migrate files that landed in the root of seagatefatgreenNADD1400music
into the music/ subdirectory.
"""
from __future__ import annotations

import argparse
import hashlib
import logging
import os
import sys
import time
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")
logger = logging.getLogger("migrate")

ROOT = Path("/media/seagatefatgreenNADD1400music")
TARGET = ROOT / "music"
PROTECTED = {
    "data", "memory", "models", "music", "playlists", "zips",
    ".Trash-1000", ".musicstream-ready", "System Volume Information"
}


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as f:
        while chunk := f.read(1024 * 1024):
            h.update(chunk)
    return h.hexdigest()


def main():
    parser = argparse.ArgumentParser(description="Migrate rogue root downloads to music folder")
    parser.add_argument("--dry-run", action="store_true", help="Scan only, do not move files")
    parser.add_argument("--root", type=Path, default=ROOT, help=f"Root directory of external drive (default: {ROOT})")
    parser.add_argument("--target", type=Path, default=None, help="Target music directory (default: <root>/music)")
    args = parser.parse_args()

    root = args.root.resolve()
    target = (args.target or (root / "music")).resolve()

    if not root.is_dir():
        logger.error("Root directory %s does not exist", root)
        sys.exit(1)
    if not target.is_dir():
        logger.error("Target directory %s does not exist", target)
        sys.exit(1)

    artist_dirs = [p for p in root.iterdir() if p.name not in PROTECTED]
    logger.info("Found %d rogue items in root %s", len(artist_dirs), root)

    total_files_to_move = 0
    total_whole_dirs = 0
    total_merges = 0
    total_quarantined = 0
    total_identical = 0
    now_ts = int(time.time())

    for idx, artist_path in enumerate(sorted(artist_dirs, key=lambda p: p.name)):
        dest_artist = target / artist_path.name

        if not dest_artist.exists():
            total_whole_dirs += 1
            if not args.dry_run:
                artist_path.rename(dest_artist)
            continue

        total_merges += 1
        all_files = [p for p in artist_path.rglob("*") if p.is_file()]
        for src_file in all_files:
            rel = src_file.relative_to(artist_path)
            dest_file = dest_artist / rel

            if dest_file.exists():
                src_size = src_file.stat().st_size
                dest_size = dest_file.stat().st_size
                if src_size == dest_size and sha256(src_file) == sha256(dest_file):
                    total_identical += 1
                    if not args.dry_run:
                        src_file.unlink()
                else:
                    total_quarantined += 1
                    total_files_to_move += 1
                    if not args.dry_run:
                        quarantine_path = dest_file.parent / f"{dest_file.name}.orphan-{now_ts}"
                        dest_file.rename(quarantine_path)
                        dest_file.parent.mkdir(parents=True, exist_ok=True)
                        src_file.rename(dest_file)
            else:
                total_files_to_move += 1
                if not args.dry_run:
                    dest_file.parent.mkdir(parents=True, exist_ok=True)
                    src_file.rename(dest_file)

        if not args.dry_run:
            for root_dir, dirs, files in os.walk(str(artist_path), topdown=False):
                if not files and not dirs:
                    try:
                        os.rmdir(root_dir)
                    except OSError:
                        pass
            if artist_path.exists() and not any(artist_path.iterdir()):
                try:
                    artist_path.rmdir()
                except OSError:
                    pass

        if (idx + 1) % 50 == 0:
            logger.info("Processed %d / %d artist dirs...", idx + 1, len(artist_dirs))

    logger.info("Summary (dry_run=%s):", args.dry_run)
    logger.info("  Whole dirs renamed: %d", total_whole_dirs)
    logger.info("  Dirs merged: %d", total_merges)
    logger.info("  Files moved: %d", total_files_to_move)
    logger.info("  Older versions quarantined: %d", total_quarantined)
    logger.info("  Identical files deduplicated: %d", total_identical)

    if not args.dry_run:
        remaining = [p.name for p in root.iterdir() if p.name not in PROTECTED]
        logger.info("Remaining rogue items in %s: %d", root, len(remaining))
        if remaining:
            logger.warning("Unmerged items: %s", remaining[:10])


if __name__ == "__main__":
    main()
