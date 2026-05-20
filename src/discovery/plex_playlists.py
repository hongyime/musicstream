"""
discovery/plex_playlists.py — Create/update weekly Plex discovery playlists

After each ListenBrainz discovery batch, this module creates or updates a
Plex playlist named ``Discovered: Y{year} W{week}`` (e.g. "Discovered: Y2026 W18")
containing all downloaded tracks that originated from lb_recommendations for
that ISO calendar week.

Plex API calls:
  POST /playlists                — create a new playlist
  PUT  /playlists/{id}/items     — add items to an existing playlist

Environment variables:
  PLEX_TOKEN              — Plex authentication token
  PLEX_URL                — Base URL for Plex (default: http://localhost:32400)
  PLEX_LIBRARY_SECTION_ID — Library section ID for the music library

Playlist Naming:
  - Format: "Discovered: Y2026 W18" (ISO week number)
  - Updated weekly after each ListenBrainz discovery run
  - ListenBrainz discovery runs daily but playlists group by week
"""

from __future__ import annotations

import logging
import os
from datetime import datetime as dt, timezone, timedelta
from typing import Optional

import requests
from sqlalchemy.orm import Session

from src.models import LbRecommendation, Track, TrackStatus

logger = logging.getLogger(__name__)

# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_PLEX_URL = "http://localhost:32400"


# ── Main playlist sync class ──────────────────────────────────────────────────

class PlexPlaylistSync:
    """
    Creates or updates weekly Plex playlists from ListenBrainz discoveries.

    Usage::

        sync = PlexPlaylistSync()
        sync.sync_discovery_playlist(session, month="May", year=2026)  # month/year ignored
    
    Note:
        Month and year parameters are maintained for backward compatibility but
        ignored. The method always creates playlists for the current ISO week:
        "Discovered: Y2026 W18"
    """

    def __init__(
        self,
        plex_url: Optional[str] = None,
        plex_token: Optional[str] = None,
        library_section_id: Optional[str] = None,
    ) -> None:
        self._plex_url          = (plex_url or os.environ.get("PLEX_URL", DEFAULT_PLEX_URL)).rstrip("/")
        self._plex_token        = plex_token or os.environ.get("PLEX_TOKEN", "")
        self._section_id        = library_section_id or os.environ.get("PLEX_LIBRARY_SECTION_ID", "")

        self._session = requests.Session()
        if self._plex_token:
            self._session.headers.update({"X-Plex-Token": self._plex_token})
        self._session.headers.update({"Accept": "application/json"})

    # ── Public API ─────────────────────────────────────────────────────────────

    def sync_discovery_playlist(
        self,
        session: Session,
        month: str,
        year: int,
    ) -> None:
        """
        Create or update the Plex playlist ``Discovered: {Month} {Year}``.

        Finds all downloaded tracks that were ingested from lb_recommendations
        during *month*/*year*, then either creates a new playlist or appends
        any missing tracks to the existing one.

        Args:
            session: SQLAlchemy session.
            month:   Full month name, e.g. "May".
            year:    Four-digit year, e.g. 2026.
        
        Note:
            Month/year parameters maintained for backward compatibility but ignored
            in favor of weekly-based naming. Playlist now follows format: "Discovered: Y{year} W{week}"
        """
        from datetime import datetime as dt
        # ListenBrainz runs daily, so weekly playlists make sense
        week_num = dt.now().isocalendar()[1]  # ISO week number (1-53)
        playlist_name = f"Discovered: Y{year} W{week_num}"
        logger.info("Syncing Plex playlist: %r", playlist_name)

        # ── Find downloaded tracks for this month ──────────────────────────
        track_keys = self._get_downloaded_track_keys(session, month, year)
        if not track_keys:
            logger.info("No downloaded LB tracks for %s %d; skipping playlist sync", month, year)
            return

        logger.info(
            "Found %d downloaded LB tracks for %s %d",
            len(track_keys), month, year,
        )

        # ── Check if playlist already exists ──────────────────────────────
        existing_playlist_id = self._find_playlist(playlist_name)

        if existing_playlist_id is None:
            # Create new playlist
            self._create_playlist(playlist_name, track_keys)
        else:
            # Update existing playlist with any new tracks
            self._update_playlist(existing_playlist_id, track_keys)

    # ── Internal: query DB for downloaded tracks ───────────────────────────────

    def _get_downloaded_track_keys(
        self,
        session: Session,
        month: str,
        year: int,
    ) -> list[str]:
        """
        Return Plex rating keys (file paths) for all downloaded tracks that
        originated from lb_recommendations fetched during *month*/*year*.
        
        Note: For backward compatibility, month/year parameters are accepted but
        ignored.  The method now always queries for the current week.

        A track qualifies if:
          - Its LbRecommendation.status == 'ingested'
          - Its LbRecommendation.fetched_at falls within the current ISO week
          - Its Track.status == 'downloaded'
          - Its Track.file_path is not None
        """
        # Always query for current week regardless of passed month/year
        now = dt.now()
        year = now.year
        week_num = now.isocalendar()[1]

        # datetime.fromisocalendar() returns a naive datetime and does NOT accept
        # tzinfo as a kwarg; attach UTC after construction. timedelta is imported
        # at module level (it is NOT an attribute of the `dt` alias).
        week_start = dt.fromisocalendar(year, week_num, 1).replace(tzinfo=timezone.utc)
        week_end = week_start + timedelta(days=7)

        rows = (
            session.query(Track)
            .join(LbRecommendation, LbRecommendation.track_id == Track.id)
            .filter(
                LbRecommendation.status == "ingested",
                LbRecommendation.fetched_at >= week_start,
                LbRecommendation.fetched_at < week_end,
                Track.status == TrackStatus.DOWNLOADED.value,
                Track.file_path.isnot(None),
            )
            .all()
        )

        return [t.file_path for t in rows if t.file_path]

    # ── Internal: Plex API helpers ─────────────────────────────────────────────

    def _find_playlist(self, playlist_name: str) -> Optional[str]:
        """
        Search existing Plex playlists for one matching *playlist_name*.

        Returns the playlist ``ratingKey`` (ID) if found, else None.
        """
        url = f"{self._plex_url}/playlists"
        try:
            resp = self._session.get(url, timeout=15)
        except requests.RequestException as exc:
            logger.warning("Failed to list Plex playlists: %s", exc)
            return None

        if resp.status_code != 200:
            logger.warning("Plex /playlists returned HTTP %d", resp.status_code)
            return None

        try:
            data = resp.json()
        except ValueError:
            logger.warning("Plex /playlists returned non-JSON response")
            return None

        # Plex JSON: {"MediaContainer": {"Metadata": [{"title": ..., "ratingKey": ...}]}}
        media_container = data.get("MediaContainer", {})
        playlists = media_container.get("Metadata") or []

        for pl in playlists:
            if pl.get("title") == playlist_name:
                rating_key = pl.get("ratingKey")
                logger.debug("Found existing playlist %r with ratingKey=%s", playlist_name, rating_key)
                return str(rating_key) if rating_key else None

        return None

    def _resolve_plex_keys(self, file_paths: list[str]) -> list[str]:
        """
        Resolve file paths to Plex rating keys by searching the music library.

        Returns a list of Plex rating keys (strings) for tracks that Plex
        has indexed.  Paths that Plex cannot find are silently skipped.
        """
        if not self._section_id:
            logger.warning("PLEX_LIBRARY_SECTION_ID not set; cannot resolve Plex keys")
            return []

        rating_keys: list[str] = []

        for file_path in file_paths:
            url = f"{self._plex_url}/library/sections/{self._section_id}/search"
            params = {"type": 10, "file": file_path}  # type=10 → track
            try:
                resp = self._session.get(url, params=params, timeout=10)
            except requests.RequestException as exc:
                logger.debug("Plex search failed for %r: %s", file_path, exc)
                continue

            if resp.status_code != 200:
                logger.debug("Plex search HTTP %d for %r", resp.status_code, file_path)
                continue

            try:
                data = resp.json()
            except ValueError:
                continue

            media_container = data.get("MediaContainer", {})
            items = media_container.get("Metadata") or []
            if items:
                key = items[0].get("ratingKey")
                if key:
                    rating_keys.append(str(key))

        return rating_keys

    def _create_playlist(self, playlist_name: str, file_paths: list[str]) -> None:
        """
        Create a new Plex playlist via POST /playlists.

        Args:
            playlist_name: Display name for the playlist.
            file_paths:    List of file paths for tracks to include.
        """
        rating_keys = self._resolve_plex_keys(file_paths)
        if not rating_keys:
            logger.warning(
                "No Plex rating keys resolved for playlist %r; cannot create",
                playlist_name,
            )
            return

        # Plex expects a comma-separated list of rating keys as the uri
        # Format: library://section/{section_id}/item/{key}
        # Simpler approach: use the machineIdentifier + key format
        machine_id = self._get_machine_identifier()

        if machine_id:
            uri_items = ",".join(
                f"library://{machine_id}/item/{key}" for key in rating_keys
            )
        else:
            # Fallback: just use the rating keys directly
            uri_items = ",".join(rating_keys)

        url = f"{self._plex_url}/playlists"
        params = {
            "type": "audio",
            "title": playlist_name,
            "smart": 0,
            "uri": uri_items,
        }

        try:
            resp = self._session.post(url, params=params, timeout=15)
        except requests.RequestException as exc:
            logger.error("Failed to create Plex playlist %r: %s", playlist_name, exc)
            return

        if resp.status_code in (200, 201):
            logger.info("Created Plex playlist %r with %d tracks", playlist_name, len(rating_keys))
        else:
            logger.error(
                "Plex playlist creation failed HTTP %d: %s",
                resp.status_code,
                resp.text[:200],
            )

    def _update_playlist(self, playlist_id: str, file_paths: list[str]) -> None:
        """
        Add tracks to an existing Plex playlist via PUT /playlists/{id}/items.

        Only adds tracks not already present in the playlist.

        Args:
            playlist_id: Plex ratingKey of the existing playlist.
            file_paths:  List of file paths for tracks to add.
        """
        rating_keys = self._resolve_plex_keys(file_paths)
        if not rating_keys:
            logger.info("No new Plex rating keys to add to playlist %s", playlist_id)
            return

        # Fetch existing items to avoid duplicates
        existing_keys = self._get_playlist_item_keys(playlist_id)
        new_keys = [k for k in rating_keys if k not in existing_keys]

        if not new_keys:
            logger.info("All tracks already present in playlist %s; nothing to add", playlist_id)
            return

        machine_id = self._get_machine_identifier()

        if machine_id:
            uri_items = ",".join(
                f"library://{machine_id}/item/{key}" for key in new_keys
            )
        else:
            uri_items = ",".join(new_keys)

        url = f"{self._plex_url}/playlists/{playlist_id}/items"
        params = {"uri": uri_items}

        try:
            resp = self._session.put(url, params=params, timeout=15)
        except requests.RequestException as exc:
            logger.error("Failed to update Plex playlist %s: %s", playlist_id, exc)
            return

        if resp.status_code in (200, 201):
            logger.info(
                "Updated Plex playlist %s: added %d new tracks",
                playlist_id, len(new_keys),
            )
        else:
            logger.error(
                "Plex playlist update failed HTTP %d: %s",
                resp.status_code,
                resp.text[:200],
            )

    def _get_playlist_item_keys(self, playlist_id: str) -> set[str]:
        """
        Return the set of ratingKeys already in a playlist.
        """
        url = f"{self._plex_url}/playlists/{playlist_id}/items"
        try:
            resp = self._session.get(url, timeout=15)
        except requests.RequestException as exc:
            logger.warning("Failed to fetch playlist items for %s: %s", playlist_id, exc)
            return set()

        if resp.status_code != 200:
            logger.warning("Plex playlist items HTTP %d for playlist %s", resp.status_code, playlist_id)
            return set()

        try:
            data = resp.json()
        except ValueError:
            return set()

        media_container = data.get("MediaContainer", {})
        items = media_container.get("Metadata") or []
        return {str(item["ratingKey"]) for item in items if "ratingKey" in item}

    def _get_machine_identifier(self) -> Optional[str]:
        """
        Fetch the Plex server machine identifier from GET /.

        Returns the identifier string or None on failure.
        """
        try:
            resp = self._session.get(self._plex_url, timeout=10)
        except requests.RequestException as exc:
            logger.debug("Failed to fetch Plex machine identifier: %s", exc)
            return None

        if resp.status_code != 200:
            return None

        try:
            data = resp.json()
        except ValueError:
            return None

        return data.get("MediaContainer", {}).get("machineIdentifier")
