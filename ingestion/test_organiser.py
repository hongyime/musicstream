"""
Unit tests for musicstream/ingestion/organiser.py

Tests cover:
  - _sanitize()   : forbidden chars, length cap, leading/trailing trim
  - _build_path() : year present/absent, track_number present/absent
  - _compute_sha256() : known digest
  - _resolve_collision() : no collision, first collision, multiple collisions
"""

from __future__ import annotations

import hashlib
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

# Ensure the musicstream package root is on sys.path so that
# `from exceptions import ...` and `from models import ...` resolve correctly.
_MUSICSTREAM_ROOT = str(Path(__file__).resolve().parent.parent)
if _MUSICSTREAM_ROOT not in sys.path:
    sys.path.insert(0, _MUSICSTREAM_ROOT)

from ingestion.organiser import FileOrganiser  # noqa: E402
from models import Track  # noqa: E402


def _make_organiser() -> FileOrganiser:
    return FileOrganiser(
        media_drive="/media",
        plex_url="http://localhost:32400",
        plex_token="fake-token",
        plex_section_id="1",
    )


class _TrackStub:
    """
    Lightweight stand-in for a SQLAlchemy Track ORM object.

    Using Track.__new__ bypasses SQLAlchemy's instrumentation and causes
    AttributeError when setting mapped columns.  This plain Python object
    exposes the same attributes that FileOrganiser reads, without any ORM
    machinery.
    """
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


def _make_track(**kwargs) -> _TrackStub:
    """Return a minimal _TrackStub with sensible defaults."""
    defaults = dict(
        id=1,
        spotify_uri="spotify:track:abc",
        spotify_id="abc",
        title="My Song",
        artist="The Artist",
        album_artist="The Artist",
        album="Great Album",
        year="2024",
        track_number=3,
        format="flac",
        status="pending",
    )
    defaults.update(kwargs)
    return _TrackStub(**defaults)


class TestSanitize(unittest.TestCase):
    def setUp(self):
        self.org = _make_organiser()

    def test_replaces_forbidden_chars(self):
        raw = 'a<b>c:d"e/f\\g|h?i*j'
        result = self.org._sanitize(raw)
        self.assertEqual(result, "a_b_c_d_e_f_g_h_i_j")

    def test_max_200_chars(self):
        long_name = "x" * 300
        result = self.org._sanitize(long_name)
        self.assertEqual(len(result), 200)

    def test_strips_leading_trailing_periods_and_spaces(self):
        self.assertEqual(self.org._sanitize("  .hello.  "), "hello")
        self.assertEqual(self.org._sanitize("...title..."), "title")
        self.assertEqual(self.org._sanitize("   spaces   "), "spaces")

    def test_normal_name_unchanged(self):
        self.assertEqual(self.org._sanitize("Normal Name"), "Normal Name")

    def test_empty_string(self):
        # After stripping an all-period/space string we get empty — that's fine
        result = self.org._sanitize("   ")
        self.assertEqual(result, "")


class TestBuildPath(unittest.TestCase):
    def setUp(self):
        self.org = _make_organiser()

    def _p(self, *parts: str) -> str:
        return os.path.join(*parts)

    def test_full_path_with_year_and_track_number(self):
        track = _make_track(
            album_artist="The Artist",
            album="Great Album",
            year="2024",
            track_number=3,
            title="My Song",
        )
        path = self.org._build_path(track, ".flac")
        expected = self._p("/media", "The Artist", "Great Album (2024)", "03 - My Song.flac")
        self.assertEqual(path, expected)

    def test_year_omitted_when_empty(self):
        track = _make_track(year=None, track_number=1, title="Track One")
        path = self.org._build_path(track, ".mp3")
        # Album folder should NOT contain parenthesised year
        album_folder = Path(path).parent.name
        self.assertNotIn("(", album_folder)
        self.assertNotIn(")", album_folder)
        self.assertTrue(os.path.basename(path) == "01 - Track One.mp3")

    def test_year_omitted_when_empty_string(self):
        track = _make_track(year="", track_number=1, title="Track One")
        path = self.org._build_path(track, ".mp3")
        album_folder = Path(path).parent.name
        self.assertNotIn("(", album_folder)

    def test_track_number_omitted_when_none(self):
        track = _make_track(track_number=None, title="Untitled")
        path = self.org._build_path(track, ".flac")
        filename = os.path.basename(path)
        # Should be just "Untitled.flac", no "NN - " prefix
        self.assertEqual(filename, "Untitled.flac")

    def test_track_number_zero_padded(self):
        track = _make_track(track_number=7, title="Lucky")
        path = self.org._build_path(track, ".flac")
        filename = os.path.basename(path)
        self.assertTrue(filename.startswith("07 - "))

    def test_track_number_two_digits_not_padded_further(self):
        track = _make_track(track_number=12, title="Twelve")
        path = self.org._build_path(track, ".flac")
        filename = os.path.basename(path)
        self.assertTrue(filename.startswith("12 - "))

    def test_album_artist_falls_back_to_artist(self):
        track = _make_track(album_artist=None, artist="Solo Artist")
        path = self.org._build_path(track, ".mp3")
        parts = Path(path).parts
        # Second part (after media_drive root) should be the artist folder
        self.assertEqual(parts[2], "Solo Artist")

    def test_forbidden_chars_in_metadata_are_sanitized(self):
        track = _make_track(
            album_artist='AC/DC',
            album='Highway: To Hell',
            title='Back in Black',
            year="1980",
            track_number=1,
        )
        path = self.org._build_path(track, ".mp3")
        # No raw forbidden chars should appear in the path components
        for part in Path(path).parts[1:]:  # skip drive root
            self.assertNotIn("/", part)
            self.assertNotIn(":", part)


class TestComputeSha256(unittest.TestCase):
    def setUp(self):
        self.org = _make_organiser()

    def test_known_digest(self):
        data = b"hello world"
        expected = hashlib.sha256(data).hexdigest()
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            tmp_path = f.name
        try:
            result = self.org._compute_sha256(tmp_path)
            self.assertEqual(result, expected)
        finally:
            os.unlink(tmp_path)

    def test_empty_file(self):
        expected = hashlib.sha256(b"").hexdigest()
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp_path = f.name
        try:
            result = self.org._compute_sha256(tmp_path)
            self.assertEqual(result, expected)
        finally:
            os.unlink(tmp_path)


class TestResolveCollision(unittest.TestCase):
    def setUp(self):
        self.org = _make_organiser()

    def _mock_session(self, existing_paths: dict[str, int]) -> MagicMock:
        """
        Build a mock session whose query().filter().first() returns a Track
        stub when the queried file_path is in *existing_paths*, else None.

        We capture the path value by inspecting the SQLAlchemy BinaryExpression
        that is passed to filter().  The right-hand side of
        ``Track.file_path == path`` is a BindParameter whose ``.value``
        attribute holds the literal string.
        """
        session = MagicMock()

        def _first_side_effect():
            filter_call = session.query.return_value.filter.call_args
            clause = filter_call[0][0]
            # SQLAlchemy BinaryExpression: clause.right is a BindParameter
            path_value = clause.right.value
            if path_value in existing_paths:
                stub = MagicMock()
                stub.id = existing_paths[path_value]
                return stub
            return None

        session.query.return_value.filter.return_value.first.side_effect = _first_side_effect
        return session

    def _p(self, *parts: str) -> str:
        """Build a platform-native path from parts."""
        return os.path.join(*parts)

    def test_no_collision_returns_original(self):
        base = self._p(os.sep + "media", "A", "B", "01 - Song.flac")
        session = self._mock_session({})
        result = self.org._resolve_collision(base, 1, session)
        self.assertEqual(result, base)

    def test_collision_with_different_track_appends_suffix(self):
        base = self._p(os.sep + "media", "A", "B", "01 - Song.flac")
        expected = self._p(os.sep + "media", "A", "B", "01 - Song (2).flac")
        # Path owned by track id=99 (different from our track id=1)
        session = self._mock_session({base: 99})
        result = self.org._resolve_collision(base, 1, session)
        self.assertEqual(result, expected)

    def test_collision_with_same_track_returns_original(self):
        base = self._p(os.sep + "media", "A", "B", "01 - Song.flac")
        # Path already owned by the same track (id=1) — not a collision
        session = self._mock_session({base: 1})
        result = self.org._resolve_collision(base, 1, session)
        self.assertEqual(result, base)

    def test_multiple_collisions_increments_counter(self):
        base = self._p(os.sep + "media", "A", "B", "01 - Song.flac")
        col2 = self._p(os.sep + "media", "A", "B", "01 - Song (2).flac")
        expected = self._p(os.sep + "media", "A", "B", "01 - Song (3).flac")
        session = self._mock_session({base: 99, col2: 100})
        result = self.org._resolve_collision(base, 1, session)
        self.assertEqual(result, expected)


if __name__ == "__main__":
    unittest.main()
