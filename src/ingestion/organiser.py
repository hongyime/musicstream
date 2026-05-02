"""
musicstream/ingestion/organiser.py — File organisation and Plex library refresh

Moves a downloaded audio file from temp/ into the Plex-compatible directory
structure on the external HDD, computes a SHA-256 checksum, updates the DB
record, and triggers a Plex library section refresh.

Directory structure:
    {media_drive}/{Album Artist}/{Album} ({Year})/{NN} - {Title}.{ext}

Rules:
  - Year is omitted from the album folder name when track.year is empty/None.
  - Track-number prefix (NN, zero-padded to 2 digits) is omitted when
    track.track_number is None → filename becomes just "{Title}.{ext}".
  - Filename sanitisation: characters  < > : " / \\ | ? *  are replaced with
    underscore, result is capped at 200 chars, and leading/trailing periods
    and spaces are stripped.
  - If the computed final path already exists in the DB (another track owns it),
    a numeric suffix " (2)", " (3)", … is appended to the stem until the path
    is unique.
  - SHA-256 is computed from the FINAL file at its FINAL path (not temp path).
  - Plex refresh is triggered via:
      POST http://{plex_url}/library/sections/{section_id}/refresh?X-Plex-Token={token}
"""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shutil
from pathlib import Path
from typing import Optional

import requests
from sqlalchemy.orm import Session

from src.exceptions import OrganiserError
from src.models import Track, TrackStatus

logger = logging.getLogger(__name__)

# Characters forbidden in file/directory names on Windows and most POSIX systems.
_FORBIDDEN_RE = re.compile(r'[<>:"/\\|?*]')


class FileOrganiser:
    """Moves tagged audio files into the Plex directory structure."""

    def __init__(
        self,
        media_drive: str,
        plex_url: str,
        plex_token: str,
        plex_section_id: str,
    ) -> None:
        """
        Parameters
        ----------
        media_drive:
            Root path of the external HDD / media drive, e.g. ``/media`` or
            ``E:\\Music``.
        plex_url:
            Base URL of the Plex Media Server, e.g. ``http://localhost:32400``.
            Must NOT have a trailing slash.
        plex_token:
            Plex authentication token (``X-Plex-Token``).
        plex_section_id:
            Numeric ID of the Plex music library section to refresh.
        """
        self._media_drive = media_drive.rstrip("/\\")
        self._plex_url = plex_url.rstrip("/")
        self._plex_token = plex_token
        self._plex_section_id = plex_section_id

    # ── Public API ─────────────────────────────────────────────────────────────

    def organise(self, temp_path: str, track: Track, session: Session) -> str:
        """
        Move *temp_path* into the Plex directory structure.

        Steps:
          1. Determine the target extension from *temp_path*.
          2. Build the canonical final path via ``_build_path()``.
          3. Resolve any file-path collision in the DB.
          4. Create parent directories.
          5. Move the file with ``shutil.move()``.
          6. Compute SHA-256 of the final file.
          7. Update the DB record.
          8. Trigger a Plex library refresh.

        Returns
        -------
        str
            The absolute path where the file now lives.

        Raises
        ------
        OrganiserError
            If the move fails or the DB update cannot be completed.
        """
        ext = Path(temp_path).suffix.lower()  # e.g. ".flac" or ".mp3"
        fmt = ext.lstrip(".")                  # "flac" or "mp3"

        final_path = self._build_path(track, ext)
        final_path = self._resolve_collision(final_path, track.id, session)

        # Create parent directories
        try:
            os.makedirs(os.path.dirname(final_path), exist_ok=True)
        except OSError as exc:
            raise OrganiserError(
                f"Cannot create directory for {final_path!r}: {exc}"
            ) from exc

        # Move file
        try:
            shutil.move(temp_path, final_path)
            logger.info("Moved %r → %r", temp_path, final_path)
        except (OSError, shutil.Error) as exc:
            raise OrganiserError(
                f"Failed to move {temp_path!r} to {final_path!r}: {exc}"
            ) from exc

        # Compute SHA-256 from the FINAL file at its FINAL path
        sha256 = self._compute_sha256(final_path)
        size = os.path.getsize(final_path)

        # Persist to DB
        self._update_db(session, track, final_path, sha256, size, fmt)

        # Trigger Plex refresh (non-fatal on failure)
        try:
            self._refresh_plex()
        except Exception as exc:  # noqa: BLE001
            logger.warning("Plex refresh failed (non-fatal): %s", exc)

        return final_path

    # ── Path building ──────────────────────────────────────────────────────────

    def _build_path(self, track: Track, ext: Optional[str] = None) -> str:
        """
        Build the canonical destination path for *track*.

        Format::

            {media_drive}/{Album Artist}/{Album} ({Year})/{NN} - {Title}.{ext}

        - Year is omitted when ``track.year`` is falsy.
        - ``NN`` prefix is omitted when ``track.track_number`` is ``None``.
        - Every path component is sanitised via ``_sanitize()``.

        Parameters
        ----------
        track:
            The ORM Track object.
        ext:
            File extension including the leading dot, e.g. ``".flac"``.
            When ``None`` the extension is derived from ``track.format``
            (``"flac"`` → ``".flac"``, ``"mp3"`` → ``".mp3"``).

        Returns
        -------
        str
            Absolute path string (not yet guaranteed to be unique in the DB).
        """
        if ext is None:
            fmt = track.format or "mp3"
            ext = f".{fmt}"

        # ── Album Artist folder ────────────────────────────────────────────────
        album_artist = track.album_artist or track.artist or "Unknown Artist"
        artist_folder = self._sanitize(album_artist)

        # ── Album folder ───────────────────────────────────────────────────────
        album = track.album or "Unknown Album"
        if track.year:
            album_folder = self._sanitize(f"{album} ({track.year})")
        else:
            album_folder = self._sanitize(album)

        # ── Filename ───────────────────────────────────────────────────────────
        title = track.title or "Unknown Title"
        if track.track_number is not None:
            nn = str(track.track_number).zfill(2)
            filename = self._sanitize(f"{nn} - {title}") + ext
        else:
            filename = self._sanitize(title) + ext

        return os.path.join(self._media_drive, artist_folder, album_folder, filename)

    # ── Sanitisation ───────────────────────────────────────────────────────────

    def _sanitize(self, name: str) -> str:
        """
        Sanitise a file or directory name component.

        - Replaces ``< > : " / \\ | ? *`` with ``_``.
        - Truncates to 200 characters.
        - Strips leading and trailing periods and spaces.

        Parameters
        ----------
        name:
            Raw string to sanitise.

        Returns
        -------
        str
            Sanitised string safe for use as a path component.
        """
        sanitised = _FORBIDDEN_RE.sub("_", name)
        sanitised = sanitised[:200]
        sanitised = sanitised.strip(". ")
        return sanitised

    # ── SHA-256 ────────────────────────────────────────────────────────────────

    def _compute_sha256(self, path: str) -> str:
        """
        Compute the SHA-256 hex digest of the file at *path*.

        Reads the file in 64 KiB chunks to avoid loading large audio files
        entirely into memory.

        Parameters
        ----------
        path:
            Absolute path to the file.

        Returns
        -------
        str
            Lowercase hex-encoded SHA-256 digest.

        Raises
        ------
        OrganiserError
            If the file cannot be read.
        """
        h = hashlib.sha256()
        try:
            with open(path, "rb") as fh:
                for chunk in iter(lambda: fh.read(65536), b""):
                    h.update(chunk)
        except OSError as exc:
            raise OrganiserError(f"Cannot read file for SHA-256: {path!r}: {exc}") from exc
        return h.hexdigest()

    # ── Plex refresh ───────────────────────────────────────────────────────────

    def _refresh_plex(self) -> None:
        """
        Trigger a Plex library section refresh.

        Sends::

            POST http://{plex_url}/library/sections/{section_id}/refresh
                 ?X-Plex-Token={token}

        A non-2xx response is logged as a warning but does NOT raise an
        exception — a Plex refresh failure must never abort the pipeline.
        """
        url = (
            f"{self._plex_url}/library/sections/{self._plex_section_id}/refresh"
        )
        params = {"X-Plex-Token": self._plex_token}
        try:
            resp = requests.post(url, params=params, timeout=10)
            if resp.ok:
                logger.info(
                    "Plex library section %s refresh triggered (HTTP %s).",
                    self._plex_section_id,
                    resp.status_code,
                )
            else:
                logger.warning(
                    "Plex refresh returned HTTP %s: %s",
                    resp.status_code,
                    resp.text[:200],
                )
        except requests.RequestException as exc:
            logger.warning("Plex refresh request failed: %s", exc)

    # ── DB update ──────────────────────────────────────────────────────────────

    def _update_db(
        self,
        session: Session,
        track: Track,
        final_path: str,
        sha256: str,
        size: int,
        fmt: str,
    ) -> None:
        """
        Persist file metadata and pipeline state to the database.

        Sets:
          - ``file_path``       → *final_path*
          - ``file_sha256``     → *sha256*
          - ``file_size_bytes`` → *size*
          - ``status``          → ``'downloaded'``
          - ``format``          → ``'flac'`` or ``'mp3'``

        Parameters
        ----------
        session:
            Active SQLAlchemy session (caller is responsible for commit).
        track:
            The ORM Track object to update.
        final_path:
            Absolute path where the file now lives.
        sha256:
            Hex-encoded SHA-256 digest of the final file.
        size:
            File size in bytes.
        fmt:
            Audio format string: ``'flac'`` or ``'mp3'``.
        """
        track.file_path = final_path
        track.file_sha256 = sha256
        track.file_size_bytes = size
        track.status = TrackStatus.DOWNLOADED.value
        track.format = fmt
        session.add(track)
        logger.debug(
            "DB updated for track %d: path=%r sha256=%s… size=%d fmt=%s",
            track.id,
            final_path,
            sha256[:12],
            size,
            fmt,
        )

    # ── Collision resolution ───────────────────────────────────────────────────

    def _resolve_collision(
        self, proposed_path: str, track_id: Optional[int], session: Session
    ) -> str:
        """
        Ensure *proposed_path* is not already owned by a different track in the DB.

        If a collision is detected the stem is suffixed with `` (2)``, `` (3)``,
        … until a free path is found.

        Parameters
        ----------
        proposed_path:
            The initially computed destination path.
        track_id:
            The ``id`` of the track being organised (may be ``None`` for new
            tracks not yet persisted).  A path owned by *this* track is not
            considered a collision.
        session:
            Active SQLAlchemy session used for the uniqueness query.

        Returns
        -------
        str
            A path that is not currently assigned to any other track in the DB.
        """
        path = proposed_path
        stem_path = Path(proposed_path)
        stem = stem_path.stem
        ext = stem_path.suffix
        parent = str(stem_path.parent)

        counter = 2
        while True:
            existing = (
                session.query(Track)
                .filter(Track.file_path == path)
                .first()
            )
            if existing is None or existing.id == track_id:
                # Path is free (or already owned by this track)
                return path
            # Collision with a different track — try next suffix
            new_stem = f"{stem} ({counter})"
            path = os.path.join(parent, new_stem + ext)
            counter += 1
