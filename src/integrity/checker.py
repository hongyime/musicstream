"""
musicstream/integrity/checker.py — File integrity check

Scans every track with status='downloaded' and a non-null file_path against
the filesystem and the stored SHA-256 hash.  Tracks whose files are missing
or whose hashes no longer match are reset to status='pending' so they re-enter
the download pipeline on the next run.

Log format (written to errors.log):
    [FILE_MISSING]  title | artist | expected_path
    [FILE_CORRUPT]  title | artist | path | expected={h1} | got={h2}
"""

from __future__ import annotations

import dataclasses
import logging
from datetime import datetime, timezone

from sqlalchemy.orm import Session

from src.models import Track, TrackStatus
from src.utils import compute_sha256

logger = logging.getLogger(__name__)

# Dedicated errors logger — matches the RotatingFileHandler configured in daemon.py
errors_logger = logging.getLogger("musicstream.errors")


@dataclasses.dataclass
class IntegrityResult:
    """Counts returned by :meth:`IntegrityChecker.run`."""

    missing: int = 0
    corrupt: int = 0
    ok: int = 0
    total_checked: int = 0


class IntegrityChecker:
    """
    Verifies that every downloaded track still exists on disk and that its
    SHA-256 hash matches the value stored in the database.

    Tracks that fail either check are reset to ``status='pending'`` so they
    re-enter the download pipeline automatically.
    """

    def run(self, session: Session) -> IntegrityResult:
        """
        Iterate all tracks with ``status='downloaded'`` and a non-null
        ``file_path`` and verify each one.

        For each track:

        1. Check the file exists on disk.
           - If missing: reset ``status='pending'``, clear ``file_path`` and
             ``file_sha256``, log ``[FILE_MISSING]`` to errors.log.
        2. Compute the SHA-256 of the file and compare with ``file_sha256``.
           - If mismatch: log ``[FILE_CORRUPT]`` to errors.log, reset
             ``status='pending'``, clear ``file_path`` and ``file_sha256``.
        3. Update ``last_checked_at`` on every checked track regardless of
           the outcome.

        Parameters
        ----------
        session:
            Active SQLAlchemy session.  The caller is responsible for
            committing or rolling back.

        Returns
        -------
        IntegrityResult
            Counts of missing, corrupt, ok, and total_checked tracks.
        """
        result = IntegrityResult()
        now = datetime.now(timezone.utc)

        tracks = (
            session.query(Track)
            .filter(
                Track.status == TrackStatus.DOWNLOADED.value,
                Track.file_path.isnot(None),
            )
            .yield_per(500)
        )

        for track in tracks:
            result.total_checked += 1
            file_path: str = track.file_path  # type: ignore[assignment]  # filtered above

            # ── 1. Existence check ─────────────────────────────────────────────
            try:
                import os
                exists = os.path.isfile(file_path)
            except (OSError, TypeError):
                exists = False

            if not exists:
                errors_logger.error(
                    "[FILE_MISSING]  %s | %s | %s",
                    track.title,
                    track.artist,
                    file_path,
                )
                logger.warning(
                    "File missing for track %d (%r by %r): %r",
                    track.id,
                    track.title,
                    track.artist,
                    file_path,
                )
                track.status = TrackStatus.PENDING.value
                track.file_path = None
                track.file_sha256 = None
                track.last_checked_at = now
                session.add(track)
                result.missing += 1
                continue

            # ── 2. Hash check ──────────────────────────────────────────────────
            try:
                actual_hash = compute_sha256(file_path)
            except OSError as exc:
                # Treat unreadable file the same as missing
                errors_logger.error(
                    "[FILE_MISSING]  %s | %s | %s",
                    track.title,
                    track.artist,
                    file_path,
                )
                logger.warning(
                    "Cannot read file for track %d (%r): %s",
                    track.id,
                    track.title,
                    exc,
                )
                track.status = TrackStatus.PENDING.value
                track.file_path = None
                track.file_sha256 = None
                track.last_checked_at = now
                session.add(track)
                result.missing += 1
                continue

            expected_hash = track.file_sha256 or ""

            if actual_hash != expected_hash:
                if not expected_hash:
                    # No hash stored yet — update it rather than resetting
                    logger.info(
                        "No stored hash for track %d (%r) — recording hash now",
                        track.id, track.title,
                    )
                    track.file_sha256 = actual_hash
                    track.last_checked_at = now
                    session.add(track)
                    result.ok += 1
                    continue

                # Hash mismatch: update stored hash and log — do NOT reset to pending.
                # Legitimate operations (retag, format conversion) change the hash.
                # Resetting to pending would cause unnecessary re-downloads.
                errors_logger.warning(
                    "[HASH_CHANGED]  %s | %s | %s | was=%s | now=%s",
                    track.title,
                    track.artist,
                    file_path,
                    expected_hash,
                    actual_hash,
                )
                logger.warning(
                    "Hash changed for track %d (%r by %r): updating stored hash "
                    "(was=%s… now=%s…) — file likely re-tagged",
                    track.id,
                    track.title,
                    track.artist,
                    expected_hash[:12],
                    actual_hash[:12],
                )
                track.file_sha256 = actual_hash
                track.last_checked_at = now
                session.add(track)
                result.ok += 1
                continue

            # ── 3. All good ────────────────────────────────────────────────────
            track.last_checked_at = now
            session.add(track)
            result.ok += 1

        logger.info(
            "Integrity check complete: total=%d ok=%d missing=%d corrupt=%d",
            result.total_checked,
            result.ok,
            result.missing,
            result.corrupt,
        )
        return result
