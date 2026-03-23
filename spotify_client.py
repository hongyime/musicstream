from __future__ import annotations

import logging
import random
import time
from typing import Dict, List

import requests  # type: ignore[import-untyped]
import spotipy  # type: ignore[import-untyped]
from spotipy.oauth2 import SpotifyPKCE  # type: ignore[import-untyped]

from exceptions import SpotifyRateLimitError

logger = logging.getLogger(__name__)

_MAX_RETRIES = 5
_BACKOFF_BASE = 3
_REQUESTS_TIMEOUT = 30
_JITTER_MAX = 2  # Maximum jitter in seconds

_SCOPES = "playlist-read-private playlist-read-collaborative user-library-read"


class SpotifyIngestor:
    def __init__(
        self,
        client_id: str,
        redirect_uri: str = "http://127.0.0.1:8888/callback",
    ) -> None:
        self.sp = spotipy.Spotify(
            auth_manager=SpotifyPKCE(
                client_id=client_id,
                redirect_uri=redirect_uri,
                scope=_SCOPES,
                cache_path=".spotify_cache",
            ),
            requests_timeout=_REQUESTS_TIMEOUT,
        )

    # ── Discovery ─────────────────────────────────────────────────────────

    def get_all_playlists(self) -> List[Dict]:
        playlists: List[Dict] = []
        offset = 0
        limit = 50

        while True:
            result = self._call_with_backoff(
                self.sp.current_user_playlists, limit=limit, offset=offset
            )
            if result is None:
                break
            for item in result["items"]:
                playlists.append({
                    "spotify_id": item["id"],
                    "name": item["name"],
                    "owner": item["owner"]["display_name"],
                    "total_tracks": item["tracks"]["total"],
                })
            if result["next"] is None:
                break
            offset += limit

        return playlists

    def get_liked_songs(self) -> List[Dict]:
        tracks: List[Dict] = []
        offset = 0
        limit = 50

        while True:
            result = self._call_with_backoff(
                self.sp.current_user_saved_tracks, limit=limit, offset=offset
            )
            if result is None:
                break
            for item in result["items"]:
                track = item["track"]
                if track:
                    tracks.append(self._extract_track_data(track))
            if result["next"] is None:
                break
            offset += limit

        return tracks

    # ── Playlist tracks ───────────────────────────────────────────────────

    def get_playlist_tracks(self, playlist_id: str) -> List[Dict]:
        tracks: List[Dict] = []
        offset = 0
        limit = 100

        while True:
            result = self._call_with_backoff(
                self.sp.playlist_tracks,
                playlist_id,
                offset=offset,
                limit=limit,
                fields="items(track(id,name,artists,uri,duration_ms,album(name),track_number)),next,total",
            )
            if result is None:
                logger.error("Failed to fetch playlist tracks after retries")
                break
            for item in result["items"]:
                track = item["track"]
                if track:
                    tracks.append(self._extract_track_data(track))
            if result["next"] is None:
                break
            offset += limit

        return tracks

    # ── Helpers ────────────────────────────────────────────────────────────

    @staticmethod
    def _extract_track_data(track: Dict) -> Dict:
        album = track.get("album") or {}
        return {
            "spotify_uri": track["uri"],
            "track_name": track["name"],
            "artist_name": ", ".join(a["name"] for a in track["artists"]),
            "album_name": album.get("name"),
            "track_number": track.get("track_number"),
            "duration_ms": track["duration_ms"],
        }

    @staticmethod
    def _call_with_backoff(func, *args, **kwargs):  # type: ignore[no-untyped-def]
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                return func(*args, **kwargs)
            except spotipy.exceptions.SpotifyException as exc:
                if exc.http_status == 429:
                    retry_after = int(exc.headers.get("Retry-After", 0)) if exc.headers else 0
                    base_wait = _BACKOFF_BASE ** attempt
                    wait = max(retry_after, base_wait) + random.uniform(0, _JITTER_MAX)
                    logger.warning(
                        "Spotify rate-limited (attempt %d/%d). Waiting %.1fs...",
                        attempt, _MAX_RETRIES, wait,
                    )
                    time.sleep(wait)
                else:
                    logger.error("Spotify API error: %s", exc)
                    raise
            except (requests.exceptions.Timeout, requests.exceptions.ConnectionError) as exc:
                wait = (_BACKOFF_BASE ** attempt) + random.uniform(0, _JITTER_MAX)
                logger.warning(
                    "Spotify network error (attempt %d/%d). Waiting %.1fs... (%s)",
                    attempt, _MAX_RETRIES, wait, exc,
                )
                time.sleep(wait)
            except requests.exceptions.RequestException as exc:
                logger.error("Spotify request failed: %s", exc)
                raise
        
        # After max retries, kill the process
        logger.error("Spotify rate limit hit after %d retries. Process terminating.", _MAX_RETRIES)
        raise SpotifyRateLimitError()
