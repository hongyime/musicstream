"""
artwork_checker.py — Embedded artwork detection helper

Checks for cover artwork embedded in audio files using mutagen.
Supports MP3 (ID3), FLAC (Vorbis), M4A (MP4) formats.

Used by daemon artwork audit endpoints.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

# ── Optional mutagen import (graceful degradation) ─────────────────────────────

try:
    from mutagen.id3 import ID3, ID3NoHeaderError
    from mutagen.flac import FLAC
    from mutagen.mp4 import MP4
    MUTAGEN_AVAILABLE = True
except ImportError:
    MUTAGEN_AVAILABLE = False
    logger.warning("mutagen not available - artwork checking will be disabled")


def check_embedded_artwork(file_path: str) -> bool:
    """
    Check if a file has embedded artwork.

    Args:
        file_path: Path to audio file (MP3, FLAC, M4A)

    Returns:
        True if artwork is embedded, False otherwise
        False if mutagen not available or file format not supported
    """
    if not MUTAGEN_AVAILABLE:
        logger.debug("mutagen not available, cannot check artwork for %s", file_path)
        return False

    path = Path(file_path)
    if not path.exists():
        logger.debug("File does not exist: %s", file_path)
        return False

    ext = path.suffix.lower()

    try:
        if ext == ".mp3":
            return _has_mp3_artwork(file_path)
        elif ext == ".flac":
            return _has_flac_artwork(file_path)
        elif ext in (".m4a", ".mp4", ".aac"):
            return _has_m4a_artwork(file_path)
        else:
            logger.debug("Unsupported file format %r for artwork check", ext)
            return False
    except Exception as e:
        logger.error("Error checking artwork in %s: %s", file_path, e)
        return False


def _has_mp3_artwork(file_path: str) -> bool:
    """
    Check MP3 file for APIC frame (ID3 artwork).

    APIC is the ID3v2.4 frame type for attached pictures.
    """
    try:
        try:
            audio = ID3(file_path)
        except ID3NoHeaderError:
            # No ID3 tags at all
            return False

        # Look for APIC frames (attached pictures)
        return bool(audio.getall("APIC"))
    except Exception as e:
        logger.debug("Error checking MP3 artwork: %s", e)
        return False


def _has_flac_artwork(file_path: str) -> bool:
    """
    Check FLAC file for embedded pictures.

    FLAC stores pictures in the METADATA_BLOCK_PICTURE block.
    """
    try:
        audio = FLAC(file_path)
        # FLAC pictures are stored in the 'pictures' attribute
        return bool(audio.pictures)
    except Exception as e:
        logger.debug("Error checking FLAC artwork: %s", e)
        return False


def _has_m4a_artwork(file_path: str) -> bool:
    """
    Check M4A/MP4 file for cover artwork.

    M4A stores artwork in the 'covr' atom (cover).
    """
    try:
        audio = MP4(file_path)
        # Check for 'covr' (cover) atom
        return 'covr' in audio
    except Exception as e:
        logger.debug("Error checking M4A artwork: %s", e)
        return False


def extract_first_artwork(file_path: str) -> Optional[bytes]:
    """
    Extract the first artwork image from a file.

    Args:
        file_path: Path to audio file

    Returns:
        Image data as bytes, or None if no artwork found
    """
    if not MUTAGEN_AVAILABLE:
        return None

    path = Path(file_path)
    if not path.exists():
        return None

    ext = path.suffix.lower()

    try:
        if ext == ".mp3":
            return _extract_mp3_artwork(file_path)
        elif ext == ".flac":
            return _extract_flac_artwork(file_path)
        elif ext in (".m4a", ".mp4", ".aac"):
            return _extract_m4a_artwork(file_path)
        else:
            return None
    except Exception as e:
        logger.error("Error extracting artwork from %s: %s", file_path, e)
        return None


def _extract_mp3_artwork(file_path: str) -> Optional[bytes]:
    """Extract artwork from APIC frame in MP3 file."""
    try:
        audio = ID3(file_path)
        apic_frames = audio.getall("APIC")
        if apic_frames:
            # Return the first APIC frame's data
            return apic_frames[0].data
        return None
    except Exception as e:
        logger.debug("Error extracting MP3 artwork: %s", e)
        return None


def _extract_flac_artwork(file_path: str) -> Optional[bytes]:
    """Extract artwork from FLAC pictures."""
    try:
        audio = FLAC(file_path)
        if audio.pictures:
            # Return the first picture's data
            return audio.pictures[0].data
        return None
    except Exception as e:
        logger.debug("Error extracting FLAC artwork: %s", e)
        return None


def _extract_m4a_artwork(file_path: str) -> Optional[bytes]:
    """Extract artwork from 'covr' atom in M4A file."""
    try:
        audio = MP4(file_path)
        if 'covr' in audio:
            # Return the first cover image
            covers = audio['covr']
            if covers:
                return covers[0]
        return None
    except Exception as e:
        logger.debug("Error extracting M4A artwork: %s", e)
        return None
