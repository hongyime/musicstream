"""
musicstream/ingestion/scraper.py — Spotify PKCE ingestion

Scrapes a user's entire Spotify library (playlists + liked songs) into
PostgreSQL using spotipy with OAuth PKCE (no client secret required).

Supports:
  - full_backfill()     : scrape everything from scratch
  - incremental_sync()  : compare snapshot_ids, fetch only changed playlists

PRD §7.3.1 album_artist rule:
  - "Various Artists" only for compilation albums (album_type == "compilation")
  - Otherwise album_artist = primary artist name
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Optional

import spotipy
from spotipy.oauth2 import SpotifyPKCE
from sqlalchemy.orm import Session

from exceptions import SpotifyRateLimitError
from models import Source, SourceType, Track, TrackStatus, track_sources
from rate_limiter import ServiceRateLimiter

logger = logging.getLogger(__name__)

# OAuth scopes required — no client secret needed for PKCE
_SCOPES = "playlist-read-private playlist-read-collaborative user-library-read"

# Sentinel spotify_id used for the liked-songs virtual source
_LIKED_SONGS_ID = "__liked_songs__"


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


class SpotifyScraper:
    """
    Spotify PKCE ingestion scraper.

    Args:
        client_id: Spotify application client ID (from .env SPOTIFY_CLIENT_ID).
    """

    def __init__(self, client_id: str) -> None:
        self._client_id = client_id
        self._rate_limiter = ServiceRateLimiter()
        self._sp: Optional[spotipy.Spotify] = None

    # ── Spotify client (lazy init) ─────────────────────────────────────────────

    @property
    def sp(self) -> spotipy.Spotify:
        """Return (or lazily create) the authenticated spotipy client."""
        if self._sp is None:
            auth_manager = SpotifyPKCE(
                client_id=self._client_id,
                redirect_uri="http://localhost:8888/callback",
                scope=_SCOPES,
                open_browser=True,
            )
            self._sp = spotipy.Spotify(auth_manager=auth_manager)
            logger.info("Spotify PKCE client initialised.")
        return self._sp

    # ── Public API ─────────────────────────────────────────────────────────────

    def full_backfill(self, session: Session) -> int:
        """
        Scrape all playlists + liked songs and upsert every track with
        status=pending (unless already downloaded).

        Returns:
            Number of newly inserted tracks.
        """
        logger.info("Starting Spotify full backfill…")
        new_count = 0

        # ── Playlists ──────────────────────────────────────────────────────────
        playlists = self.get_all_playlists()
        logger.info("Found %d playlists.", len(playlists))

        for pl in playlists:
            pl_id = pl["id"]
            pl_name = pl.get("name", pl_id)
            snapshot_id = pl.get("snapshot_id")

            source = self._get_or_create_source(
                session,
                spotify_id=pl_id,
                name=pl_name,
                source_type=SourceType.PLAYLIST.value,
            )

            raw_tracks = self.get_playlist_tracks(pl_id)
            added = self._upsert_tracks(session, raw_tracks, source)
            new_count += added

            self._update_source(session, source, snapshot_id, len(raw_tracks))
            logger.info("Playlist '%s': %d tracks upserted (%d new).", pl_name, len(raw_tracks), added)

        # ── Liked songs ────────────────────────────────────────────────────────
        liked_source = self._get_or_create_source(
            session,
            spotify_id=_LIKED_SONGS_ID,
            name="Liked Songs",
            source_type=SourceType.LIKED.value,
        )

        liked_tracks = self.get_liked_songs()
        added = self._upsert_tracks(session, liked_tracks, liked_source)
        new_count += added

        self._update_source(session, liked_source, snapshot_id=None, track_count=len(liked_tracks))
        logger.info("Liked Songs: %d tracks upserted (%d new).", len(liked_tracks), added)

        logger.info("Full backfill complete. Total new tracks: %d.", new_count)
        return new_count

    def incremental_sync(self, session: Session) -> int:
        """
        Compare snapshot_ids for each playlist source in the DB.
        Fetch only changed playlists (from stored_track_count offset onward).
        Liked Songs are always re-fetched (no snapshot_id available).

        Returns:
            Number of newly inserted tracks.
        """
        logger.info("Starting Spotify incremental sync…")
        new_count = 0

        # ── Playlists ──────────────────────────────────────────────────────────
        playlist_sources = (
            session.query(Source)
            .filter(Source.source_type == SourceType.PLAYLIST.value)
            .all()
        )

        for source in playlist_sources:
            pl_id = source.spotify_id

            # 1 lightweight API call to get current snapshot_id
            try:
                pl_meta = self._get_playlist_meta(pl_id)
            except Exception as exc:
                logger.warning("Could not fetch metadata for playlist %s: %s", pl_id, exc)
                continue

            current_snapshot = pl_meta.get("snapshot_id")

            if current_snapshot and current_snapshot == source.snapshot_id:
                logger.debug("Playlist '%s' unchanged (snapshot_id match). Skipping.", source.name)
                continue

            # Snapshot changed — fetch only new tracks from stored offset onward
            offset = source.track_count  # start from where we left off
            logger.info(
                "Playlist '%s' changed. Fetching from offset %d…",
                source.name,
                offset,
            )

            new_raw_tracks = self.get_playlist_tracks(pl_id, offset=offset)
            added = self._upsert_tracks(session, new_raw_tracks, source)
            new_count += added

            # Update snapshot_id and track_count
            new_total = source.track_count + len(new_raw_tracks)
            self._update_source(session, source, current_snapshot, new_total)
            logger.info(
                "Playlist '%s': %d new tracks added.", source.name, added
            )

        # ── Liked Songs — always re-fetch ──────────────────────────────────────
        liked_source = (
            session.query(Source)
            .filter(Source.spotify_id == _LIKED_SONGS_ID)
            .first()
        )

        if liked_source is None:
            liked_source = self._get_or_create_source(
                session,
                spotify_id=_LIKED_SONGS_ID,
                name="Liked Songs",
                source_type=SourceType.LIKED.value,
            )

        liked_tracks = self.get_liked_songs()
        added = self._upsert_tracks(session, liked_tracks, liked_source)
        new_count += added
        self._update_source(session, liked_source, snapshot_id=None, track_count=len(liked_tracks))
        logger.info("Liked Songs: %d new tracks added.", added)

        logger.info("Incremental sync complete. Total new tracks: %d.", new_count)
        return new_count

    # ── Spotify API helpers ────────────────────────────────────────────────────

    def get_all_playlists(self) -> list[dict]:
        """
        Fetch all playlists for the current user via /me/playlists (50/page).

        Returns:
            List of raw playlist dicts from the Spotify API.
        """
        playlists: list[dict] = []
        limit = 50
        offset = 0

        while True:
            attempt = 0
            while True:
                try:
                    result = self.sp.current_user_playlists(limit=limit, offset=offset)
                    self._rate_limiter.record_success("spotify")
                    break
                except spotipy.SpotifyException as exc:
                    if exc.http_status == 429:
                        retry_after = float(exc.headers.get("Retry-After", 0)) if exc.headers else 0
                        self._rate_limiter.record_failure("spotify")
                        self._rate_limiter.wait("spotify", attempt, retry_after=retry_after)
                        attempt += 1
                        if attempt >= 5:
                            raise SpotifyRateLimitError() from exc
                    else:
                        raise

            items = result.get("items") or []
            playlists.extend(items)

            if result.get("next") is None:
                break
            offset += limit

        return playlists

    def get_playlist_tracks(self, playlist_id: str, offset: int = 0) -> list[dict]:
        """
        Fetch all tracks for a playlist via /playlists/{id}/tracks (100/page).

        Args:
            playlist_id: Spotify playlist ID.
            offset:      Starting offset (used for incremental sync).

        Returns:
            List of raw playlist-track item dicts from the Spotify API.
        """
        tracks: list[dict] = []
        limit = 100
        current_offset = offset

        while True:
            attempt = 0
            while True:
                try:
                    result = self.sp.playlist_tracks(
                        playlist_id,
                        limit=limit,
                        offset=current_offset,
                    )
                    self._rate_limiter.record_success("spotify")
                    break
                except spotipy.SpotifyException as exc:
                    if exc.http_status == 429:
                        retry_after = float(exc.headers.get("Retry-After", 0)) if exc.headers else 0
                        self._rate_limiter.record_failure("spotify")
                        self._rate_limiter.wait("spotify", attempt, retry_after=retry_after)
                        attempt += 1
                        if attempt >= 5:
                            raise SpotifyRateLimitError() from exc
                    else:
                        raise

            items = result.get("items") or []
            tracks.extend(items)

            if result.get("next") is None:
                break
            current_offset += limit

        return tracks

    def get_liked_songs(self) -> list[dict]:
        """
        Fetch all liked songs via /me/tracks (50/page).

        Returns:
            List of raw saved-track item dicts from the Spotify API.
        """
        tracks: list[dict] = []
        limit = 50
        offset = 0

        while True:
            attempt = 0
            while True:
                try:
                    result = self.sp.current_user_saved_tracks(limit=limit, offset=offset)
                    self._rate_limiter.record_success("spotify")
                    break
                except spotipy.SpotifyException as exc:
                    if exc.http_status == 429:
                        retry_after = float(exc.headers.get("Retry-After", 0)) if exc.headers else 0
                        self._rate_limiter.record_failure("spotify")
                        self._rate_limiter.wait("spotify", attempt, retry_after=retry_after)
                        attempt += 1
                        if attempt >= 5:
                            raise SpotifyRateLimitError() from exc
                    else:
                        raise

            items = result.get("items") or []
            tracks.extend(items)

            if result.get("next") is None:
                break
            offset += limit

        return tracks

    # ── Metadata extraction ────────────────────────────────────────────────────

    def _extract_track_data(self, item: dict) -> Optional[dict]:
        """
        Extract normalised track metadata from a raw Spotify playlist/saved-track item.

        Handles both playlist track items (item["track"]) and saved track items
        (item["track"] is the same structure for /me/tracks).

        Returns:
            Dict with all track fields, or None if the item has no valid track
            (e.g. local files, null entries).
        """
        track = item.get("track")
        if not track or track.get("id") is None:
            # Local files or null entries — skip
            return None

        album = track.get("album") or {}
        artists = track.get("artists") or []
        album_artists = album.get("artists") or []

        # Primary artist name
        artist = artists[0]["name"] if artists else "Unknown Artist"

        # Album artist per PRD §7.3.1
        album_artist = self._album_artist_rule(album, artist)

        # Year: take first 4 chars of release_date (e.g. "2021-06-18" → "2021")
        release_date = album.get("release_date") or ""
        year = release_date[:4] if release_date else None

        # Cover art: largest image from album images list
        images = album.get("images") or []
        cover_art_url: Optional[str] = None
        if images:
            # Spotify returns images sorted largest-first
            cover_art_url = images[0].get("url")

        # ISRC from external_ids
        external_ids = track.get("external_ids") or {}
        isrc = external_ids.get("isrc")

        # Disc number (defaults to 1 in Spotify API; store as-is)
        disc_number = track.get("disc_number")

        return {
            "spotify_uri":      track["uri"],
            "spotify_id":       track["id"],
            "spotify_album_id": album.get("id"),
            "isrc":             isrc,
            "title":            track.get("name", "Unknown Title"),
            "artist":           artist,
            "album_artist":     album_artist,
            "album":            album.get("name"),
            "year":             year,
            "track_number":     track.get("track_number"),
            "disc_number":      disc_number,
            "duration_ms":      track.get("duration_ms"),
            "cover_art_url":    cover_art_url,
        }

    def _album_artist_rule(self, album: dict, artist: str) -> str:
        """
        PRD §7.3.1: Return "Various Artists" only for compilation albums.
        Otherwise return the primary artist name.

        Args:
            album:  Raw Spotify album dict.
            artist: Primary artist name already extracted from track.artists[0].

        Returns:
            "Various Artists" if album_type == "compilation", else artist.
        """
        album_type = (album.get("album_type") or "").lower()
        if album_type == "compilation":
            return "Various Artists"
        return artist

    # ── DB helpers ─────────────────────────────────────────────────────────────

    def _get_or_create_source(
        self,
        session: Session,
        spotify_id: str,
        name: str,
        source_type: str,
    ) -> Source:
        """Return existing Source or create a new one."""
        source = session.query(Source).filter(Source.spotify_id == spotify_id).first()
        if source is None:
            source = Source(
                spotify_id=spotify_id,
                name=name,
                source_type=source_type,
                track_count=0,
            )
            session.add(source)
            session.flush()  # get source.id without committing
            logger.debug("Created new source: %s (%s)", name, spotify_id)
        return source

    def _upsert_tracks(
        self,
        session: Session,
        raw_items: list[dict],
        source: Source,
    ) -> int:
        """
        Upsert tracks from raw Spotify API items into the DB.

        Rules:
          - If track does not exist: insert with status=pending.
          - If track exists with status=downloaded: leave status unchanged (7.7).
          - If track exists with any other status: update metadata, keep status.
          - Always link track to source via track_sources.

        Returns:
            Number of newly inserted tracks.
        """
        new_count = 0

        for item in raw_items:
            data = self._extract_track_data(item)
            if data is None:
                continue  # skip local files / null entries

            spotify_uri = data["spotify_uri"]

            existing = (
                session.query(Track)
                .filter(Track.spotify_uri == spotify_uri)
                .first()
            )

            if existing is None:
                # New track — insert with status=pending
                track = Track(
                    spotify_uri=spotify_uri,
                    spotify_id=data["spotify_id"],
                    spotify_album_id=data["spotify_album_id"],
                    isrc=data["isrc"],
                    title=data["title"],
                    artist=data["artist"],
                    album_artist=data["album_artist"],
                    album=data["album"],
                    year=data["year"],
                    track_number=data["track_number"],
                    disc_number=data["disc_number"],
                    duration_ms=data["duration_ms"],
                    cover_art_url=data["cover_art_url"],
                    status=TrackStatus.PENDING.value,
                )
                session.add(track)
                session.flush()  # populate track.id
                new_count += 1
                logger.debug("Inserted new track: %s — %s", data["artist"], data["title"])
            else:
                # Existing track — update metadata but NEVER reset downloaded → pending (7.7)
                existing.spotify_id = data["spotify_id"]
                existing.spotify_album_id = data["spotify_album_id"]
                existing.isrc = data["isrc"] or existing.isrc
                existing.title = data["title"]
                existing.artist = data["artist"]
                existing.album_artist = data["album_artist"]
                existing.album = data["album"]
                existing.year = data["year"]
                existing.track_number = data["track_number"]
                existing.disc_number = data["disc_number"]
                existing.duration_ms = data["duration_ms"]
                existing.cover_art_url = data["cover_art_url"] or existing.cover_art_url
                # status is intentionally NOT touched here
                track = existing

            # Link track ↔ source (idempotent — skip if already linked)
            if source not in track.sources:
                track.sources.append(source)

        return new_count

    def _update_source(
        self,
        session: Session,
        source: Source,
        snapshot_id: Optional[str],
        track_count: int,
    ) -> None:
        """
        Update snapshot_id, track_count, and last_scraped_at on a Source.

        snapshot_id is only updated when a non-None value is provided
        (liked songs have no snapshot_id).
        """
        if snapshot_id is not None:
            source.snapshot_id = snapshot_id
        source.track_count = track_count
        source.last_scraped_at = _utcnow()
        session.flush()

    def _get_playlist_meta(self, playlist_id: str) -> dict:
        """
        Fetch lightweight playlist metadata (name + snapshot_id only).
        Used by incremental_sync to check for changes with a single API call.
        """
        attempt = 0
        while True:
            try:
                result = self.sp.playlist(
                    playlist_id,
                    fields="id,name,snapshot_id",
                )
                self._rate_limiter.record_success("spotify")
                return result
            except spotipy.SpotifyException as exc:
                if exc.http_status == 429:
                    retry_after = float(exc.headers.get("Retry-After", 0)) if exc.headers else 0
                    self._rate_limiter.record_failure("spotify")
                    self._rate_limiter.wait("spotify", attempt, retry_after=retry_after)
                    attempt += 1
                    if attempt >= 5:
                        raise SpotifyRateLimitError() from exc
                else:
                    raise
