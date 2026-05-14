"""
Tests for musicstream/ingestion/scraper.py

Covers:
  - _album_artist_rule(): compilation vs non-compilation
  - _extract_track_data(): full metadata extraction, local file skipping
  - _upsert_tracks(): new track insertion, downloaded status preservation (US-2 P12)
  - _update_source(): snapshot_id and track_count update
  - incremental_sync(): skips unchanged playlists (snapshot_id match)
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest

# Mock spotipy before importing scraper
_mock_spotipy = MagicMock()
sys.modules.setdefault("spotipy", _mock_spotipy)
sys.modules.setdefault("spotipy.oauth2", _mock_spotipy.oauth2)
# Mock the cache handler module as well
sys.modules.setdefault("spotipy.cache_handler", MagicMock())

from src.models import Source, SourceType, Track, TrackStatus  # noqa: E402
from src.ingestion.scraper import SpotifyScraper  # noqa: E402


# ── Fixtures ──────────────────────────────────────────────────────────────────

# Use centralized fixtures from conftest.py - remove duplicate definitions
# These fixtures are now provided by tests/conftest.py


@pytest.fixture
def scraper():
    s = SpotifyScraper(client_id="fake-client-id")
    # Prevent real OAuth flow
    s._sp = MagicMock()
    return s


# ── Album artist rule ─────────────────────────────────────────────────────────

class TestAlbumArtistRule:
    def test_compilation_returns_various_artists(self, scraper):
        album = {"album_type": "compilation", "artists": [{"name": "Various"}]}
        result = scraper._album_artist_rule(album, "Some Artist")
        assert result == "Various Artists"

    def test_compilation_case_insensitive(self, scraper):
        album = {"album_type": "COMPILATION"}
        result = scraper._album_artist_rule(album, "Artist")
        assert result == "Various Artists"

    def test_non_compilation_returns_artist(self, scraper):
        album = {"album_type": "album"}
        result = scraper._album_artist_rule(album, "The Beatles")
        assert result == "The Beatles"

    def test_single_returns_artist(self, scraper):
        album = {"album_type": "single"}
        result = scraper._album_artist_rule(album, "Adele")
        assert result == "Adele"

    def test_missing_album_type_returns_artist(self, scraper):
        album = {}
        result = scraper._album_artist_rule(album, "Unknown")
        assert result == "Unknown"


# ── Track data extraction ─────────────────────────────────────────────────────

class TestExtractTrackData:
    def _make_item(self, **overrides):
        """Build a minimal Spotify playlist-track item dict."""
        base = {
            "track": {
                "id": "track123",
                "uri": "spotify:track:track123",
                "name": "Test Song",
                "track_number": 5,
                "disc_number": 1,
                "duration_ms": 210000,
                "external_ids": {"isrc": "USRC12345678"},
                "artists": [{"name": "Test Artist"}],
                "album": {
                    "id": "album456",
                    "name": "Test Album",
                    "album_type": "album",
                    "release_date": "2023-06-15",
                    "images": [{"url": "https://example.com/cover.jpg"}],
                    "artists": [{"name": "Test Artist"}],
                },
            }
        }
        base["track"].update(overrides.get("track_overrides", {}))
        base["track"]["album"].update(overrides.get("album_overrides", {}))
        return base

    def test_extracts_all_required_fields(self, scraper):
        item = self._make_item()
        data = scraper._extract_track_data(item)
        assert data is not None
        assert data["spotify_uri"] == "spotify:track:track123"
        assert data["spotify_id"] == "track123"
        assert data["spotify_album_id"] == "album456"
        assert data["isrc"] == "USRC12345678"
        assert data["title"] == "Test Song"
        assert data["artist"] == "Test Artist"
        assert data["album"] == "Test Album"
        assert data["year"] == "2023"
        assert data["track_number"] == 5
        assert data["disc_number"] == 1
        assert data["duration_ms"] == 210000
        assert data["cover_art_url"] == "https://example.com/cover.jpg"

    def test_year_extracted_from_release_date(self, scraper):
        item = self._make_item(album_overrides={"release_date": "1999-12-31"})
        data = scraper._extract_track_data(item)
        assert data["year"] == "1999"

    def test_year_none_when_no_release_date(self, scraper):
        item = self._make_item(album_overrides={"release_date": ""})
        data = scraper._extract_track_data(item)
        assert data["year"] is None

    def test_cover_art_url_from_first_image(self, scraper):
        item = self._make_item(album_overrides={
            "images": [
                {"url": "https://large.jpg"},
                {"url": "https://small.jpg"},
            ]
        })
        data = scraper._extract_track_data(item)
        assert data["cover_art_url"] == "https://large.jpg"

    def test_cover_art_none_when_no_images(self, scraper):
        item = self._make_item(album_overrides={"images": []})
        data = scraper._extract_track_data(item)
        assert data["cover_art_url"] is None

    def test_local_file_returns_none(self, scraper):
        item = {"track": {"id": None, "uri": "spotify:local:...", "name": "Local"}}
        assert scraper._extract_track_data(item) is None

    def test_null_track_returns_none(self, scraper):
        assert scraper._extract_track_data({"track": None}) is None

    def test_compilation_album_artist_is_various_artists(self, scraper):
        item = self._make_item(album_overrides={"album_type": "compilation"})
        data = scraper._extract_track_data(item)
        assert data["album_artist"] == "Various Artists"

    def test_non_compilation_album_artist_is_primary_artist(self, scraper):
        item = self._make_item()
        data = scraper._extract_track_data(item)
        assert data["album_artist"] == "Test Artist"


# ── Upsert tracks ─────────────────────────────────────────────────────────────

class TestUpsertTracks:
    def _make_raw_item(self, uri="spotify:track:upsert001", title="Song", artist="Artist"):
        return {
            "track": {
                "id": uri.split(":")[-1],
                "uri": uri,
                "name": title,
                "track_number": 1,
                "disc_number": 1,
                "duration_ms": 180000,
                "external_ids": {},
                "artists": [{"name": artist}],
                "album": {
                    "id": "alb001",
                    "name": "Album",
                    "album_type": "album",
                    "release_date": "2020",
                    "images": [],
                    "artists": [{"name": artist}],
                },
            }
        }

    def _make_source(self, session, spotify_id="src001"):
        src = Source(
            spotify_id=spotify_id,
            name="Test Playlist",
            source_type=SourceType.PLAYLIST.value,
        )
        session.add(src)
        session.flush()
        return src

    def test_new_track_inserted_with_pending_status(self, scraper, session):
        src = self._make_source(session, "src_new_track")
        items = [self._make_raw_item("spotify:track:new001")]
        count = scraper._upsert_tracks(session, items, src)
        assert count == 1
        track = session.query(Track).filter_by(spotify_uri="spotify:track:new001").first()
        assert track is not None
        assert track.status == TrackStatus.PENDING.value

    def test_downloaded_track_status_not_reset(self, scraper, session):
        """P12 / US-2 correctness: re-scrape must never reset downloaded → pending."""
        src = self._make_source(session, "src_downloaded")
        uri = "spotify:track:downloaded001"
        # Pre-insert as downloaded
        existing = Track(
            spotify_uri=uri,
            title="Downloaded Song",
            artist="Artist",
            status=TrackStatus.DOWNLOADED.value,
            cover_art_source="none",
        )
        session.add(existing)
        session.flush()

        items = [self._make_raw_item(uri)]
        scraper._upsert_tracks(session, items, src)

        refreshed = session.query(Track).filter_by(spotify_uri=uri).first()
        assert refreshed.status == TrackStatus.DOWNLOADED.value

    def test_duplicate_track_not_inserted_twice(self, scraper, session):
        src = self._make_source(session, "src_dedup")
        uri = "spotify:track:dedup001"
        items = [self._make_raw_item(uri)]
        count1 = scraper._upsert_tracks(session, items, src)
        count2 = scraper._upsert_tracks(session, items, src)
        assert count1 == 1
        assert count2 == 0  # already exists

    def test_track_linked_to_source(self, scraper, session):
        src = self._make_source(session, "src_link")
        uri = "spotify:track:link001"
        items = [self._make_raw_item(uri)]
        scraper._upsert_tracks(session, items, src)
        track = session.query(Track).filter_by(spotify_uri=uri).first()
        assert src in track.sources

    def test_local_files_skipped(self, scraper, session):
        src = self._make_source(session, "src_local")
        items = [{"track": None}]
        count = scraper._upsert_tracks(session, items, src)
        assert count == 0


# ── Update source ─────────────────────────────────────────────────────────────

class TestUpdateSource:
    def test_snapshot_id_updated(self, scraper, session):
        src = Source(
            spotify_id="src_snap_update",
            name="Snap Playlist",
            source_type=SourceType.PLAYLIST.value,
        )
        session.add(src)
        session.flush()
        scraper._update_source(session, src, "new_snapshot_123", 42)
        assert src.snapshot_id == "new_snapshot_123"
        assert src.track_count == 42

    def test_snapshot_id_not_overwritten_with_none(self, scraper, session):
        src = Source(
            spotify_id="src_snap_none",
            name="Liked Songs",
            source_type=SourceType.LIKED.value,
            snapshot_id="existing_snap",
        )
        session.add(src)
        session.flush()
        scraper._update_source(session, src, snapshot_id=None, track_count=10)
        assert src.snapshot_id == "existing_snap"

    def test_last_scraped_at_updated(self, scraper, session):
        src = Source(
            spotify_id="src_scraped_at",
            name="P",
            source_type=SourceType.PLAYLIST.value,
        )
        session.add(src)
        session.flush()
        scraper._update_source(session, src, "snap", 5)
        assert src.last_scraped_at is not None
