"""services/transcode.py — Quality-cutoff transcoding (SPEC.md §W3 T19 / V10).

QUALITY_CUTOFF=mp3_320 (default): any FLAC acquired by the 5-tier chain is
transcoded to MP3 320 via ffmpeg right after the organiser files it, and the
FLAC intermediate is deleted unless KEEP_FLAC_MASTER=1. QUALITY_CUTOFF=flac
keeps lossless untouched.

Failure semantics: if ffmpeg fails for any reason the ORIGINAL file is kept
and the track stays lossless — a failed downgrade must never destroy the
better source.
"""

from __future__ import annotations

import logging
import os
import subprocess

from src.core import config

logger = logging.getLogger(__name__)


def ffmpeg_available() -> bool:
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            timeout=15,
            check=True,
        )
        return True
    except (OSError, subprocess.SubprocessError):
        return False


def apply_quality_cutoff(file_path: str, track, session) -> tuple[str, str | None]:
    """Transcode ``file_path`` down to the configured cutoff when needed.

    Returns ``(path, new_format_or_None)``. ``new_format`` is set only when a
    transcode happened; callers treat ``None`` as no-op.
    """
    cutoff = getattr(config, "QUALITY_CUTOFF", "mp3_320")
    if cutoff != "mp3_320":
        return file_path, None
    if not file_path.lower().endswith(".flac"):
        return file_path, None

    base, _ = os.path.splitext(file_path)
    mp3_path = base + ".mp3"

    cmd = [
        "ffmpeg", "-hide_banner", "-loglevel", "error", "-y",
        "-i", file_path,
        "-codec:a", "libmp3lame", "-b:a", "320k",
        "-map_metadata", "0",
        "-id3v2_version", "3",
        mp3_path,
    ]
    try:
        proc = subprocess.run(cmd, capture_output=True, timeout=600)
        ok = proc.returncode == 0 and os.path.isfile(mp3_path) and os.path.getsize(mp3_path) > 0
    except (OSError, subprocess.SubprocessError) as exc:
        logger.warning(
            "[TRANSCODE] ffmpeg failed for track %d (%r): %s — keeping FLAC",
            getattr(track, "id", -1), file_path, exc,
        )
        return file_path, None

    if not ok:
        logger.warning(
            "[TRANSCODE] ffmpeg rc=%d for track %d — keeping FLAC (%s)",
            getattr(proc, "returncode", -1), getattr(track, "id", -1),
            (proc.stderr or b"")[-200:],
        )
        # Clean up a zero-byte/partial artifact if ffmpeg left one behind.
        try:
            if os.path.isfile(mp3_path):
                os.remove(mp3_path)
        except OSError:
            pass
        return file_path, None

    # ── Success: repoint the track at the MP3 ────────────────────────────────
    from src.utils import compute_sha256

    track.file_path = mp3_path
    track.format = "mp3"
    track.file_size_bytes = os.path.getsize(mp3_path)
    track.file_sha256 = compute_sha256(mp3_path)
    session.flush()

    keep_master = getattr(config, "KEEP_FLAC_MASTER", False)
    if not keep_master:
        try:
            os.remove(file_path)
        except OSError as exc:
            logger.warning(
                "[TRANSCODE] could not remove FLAC source %r: %s", file_path, exc,
            )

    logger.info(
        "[TRANSCODE] track %d: FLAC → MP3 320 (%s)",
        getattr(track, "id", -1), mp3_path,
    )
    return mp3_path, "mp3"
