"""
Tests for musicstream/exceptions.py

Verifies all required exception classes exist, inherit correctly,
and carry the right attributes.
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from exceptions import (
    DatabaseError,
    DownloadError,
    IntegrityError,
    ListenBrainzError,
    MusicBrainzError,
    MusicStreamError,
    OrganiserError,
    RateLimitError,
    SpotiFLACError,
    SpotifyRateLimitError,
    TaggingError,
    YouTubeMusicRateLimitError,
)


class TestExceptionHierarchy:
    def test_all_inherit_from_musicstream_error(self):
        for cls in [
            RateLimitError,
            SpotifyRateLimitError,
            YouTubeMusicRateLimitError,
            DownloadError,
            TaggingError,
            OrganiserError,
            IntegrityError,
            ListenBrainzError,
            MusicBrainzError,
            SpotiFLACError,
            DatabaseError,
        ]:
            assert issubclass(cls, MusicStreamError), f"{cls.__name__} must inherit MusicStreamError"

    def test_musicstream_error_inherits_exception(self):
        assert issubclass(MusicStreamError, Exception)

    def test_rate_limit_subclasses_inherit_rate_limit_error(self):
        assert issubclass(SpotifyRateLimitError, RateLimitError)
        assert issubclass(YouTubeMusicRateLimitError, RateLimitError)


class TestRateLimitError:
    def test_service_attribute(self):
        err = RateLimitError("spotify")
        assert err.service == "spotify"

    def test_message_includes_service(self):
        err = RateLimitError("spotify", "too many requests")
        assert "spotify" in str(err)
        assert "too many requests" in str(err)

    def test_default_message(self):
        err = RateLimitError("musicbrainz")
        assert "musicbrainz" in str(err)


class TestSpotifyRateLimitError:
    def test_service_is_spotify(self):
        err = SpotifyRateLimitError()
        assert err.service == "Spotify"

    def test_is_catchable_as_rate_limit_error(self):
        with pytest.raises(RateLimitError):
            raise SpotifyRateLimitError()


class TestYouTubeMusicRateLimitError:
    def test_service_is_youtube_music(self):
        err = YouTubeMusicRateLimitError()
        assert err.service == "YouTube Music"

    def test_message_contains_cookies_hint(self):
        err = YouTubeMusicRateLimitError()
        assert "cookies" in str(err).lower() or "cookie" in str(err).lower()


class TestSimpleExceptions:
    @pytest.mark.parametrize("cls", [
        DownloadError, TaggingError, OrganiserError, IntegrityError,
        ListenBrainzError, MusicBrainzError, SpotiFLACError, DatabaseError,
    ])
    def test_can_be_raised_and_caught(self, cls):
        with pytest.raises(cls):
            raise cls("test message")

    @pytest.mark.parametrize("cls", [
        DownloadError, TaggingError, OrganiserError, IntegrityError,
        ListenBrainzError, MusicBrainzError, SpotiFLACError, DatabaseError,
    ])
    def test_catchable_as_musicstream_error(self, cls):
        with pytest.raises(MusicStreamError):
            raise cls("test message")
