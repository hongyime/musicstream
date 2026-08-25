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
    no_method: int = 0
    skipped: int = 0   # §W3 V7: blocked tracks seen but left untouched


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

            # §W3 T13/V7: blocked tracks are inert — never auto-requeued.
            if track.blocked:
                result.skipped += 1
                continue

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

                # Hash mismatch: TREAT AS CORRUPTION.
                #
                # Previously this branch silently overwrote the stored hash and
                # counted the file as OK, on the theory that legitimate retags
                # change the hash. That theory is wrong here: the tagger writes
                # the hash AFTER tagging finishes (organiser/tagger handshake),
                # so the integrity checker should never see a legitimate-retag
                # mismatch. Anything that hits this branch is bit-rot, partial
                # write from a SIGKILL'd tagger, or tampering — none of which
                # we want to silently accept.
                #
                # We do NOT overwrite track.file_sha256: the original hash is
                # forensic evidence. We mark the track failed so it re-enters
                # the download queue on the next pass. Operators who know a
                # legitimate retag happened can manually clear file_sha256 to
                # force a re-record on the next integrity run (the
                # "no stored hash yet" branch above handles that case).
                errors_logger.error(
                    "[FILE_CORRUPT]  %s | %s | %s | expected=%s | got=%s",
                    track.title,
                    track.artist,
                    file_path,
                    expected_hash,
                    actual_hash,
                )
                logger.error(
                    "Hash mismatch on track %d (%r by %r): expected=%s… got=%s… "
                    "— marking corrupt, will re-download",
                    track.id,
                    track.title,
                    track.artist,
                    expected_hash[:12],
                    actual_hash[:12],
                )
                # Do NOT mutate file_sha256 — keep original for forensics.
                track.status = "pending"
                track.last_checked_at = now
                session.add(track)
                result.corrupt += 1
                continue

            # ── 3. All good ────────────────────────────────────────────────────
            track.last_checked_at = now
            session.add(track)
            result.ok += 1

        # P1-2: downloaded-without-method invariant. download_method records
        # which tier delivered the file; a downloaded row missing it means
        # unauditable provenance (cannot re-fetch by source if a tier ships
        # bad files). Observability only — we do NOT reset these rows (the
        # file is present and valid); we surface the count loudly.
        from sqlalchemy import or_
        result.no_method = (
            session.query(Track)
            .filter(
                Track.status == TrackStatus.DOWNLOADED.value,
                or_(Track.download_method.is_(None), Track.download_method == ""),
            )
            .count()
        )
        if result.no_method:
            errors_logger.error(
                "[NO_METHOD] %d downloaded track(s) have NULL/empty download_method "
                "(unauditable provenance — run the download_method backfill)",
                result.no_method,
            )

        logger.info(
            "Integrity check complete: total=%d ok=%d missing=%d corrupt=%d no_method=%d skipped_blocked=%d",
            result.total_checked,
            result.ok,
            result.missing,
            result.corrupt,
            result.no_method,
            result.skipped,
        )

        # §W3 T17: immediate alert when corruption/missing files are found.
        if result.missing or result.corrupt:
            try:
                from src.services.notify import notify_failure
                notify_failure(
                    "Library integrity problems detected",
                    detail=f"missing={result.missing} corrupt={result.corrupt}",
                )
            except Exception as exc:
                logger.debug("integrity webhook skipped: %s", exc)
        return result
