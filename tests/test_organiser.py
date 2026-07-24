"""
Tests for musicstream/ingestion/organiser.py

Covers all correctness properties from the spec:
  - _sanitize(): forbidden chars, length cap, leading/trailing trim
  - _build_path(): year present/absent, track_number present/absent,
                   album_artist fallback, forbidden chars in metadata
  - _compute_sha256(): known digest, empty file
  - _resolve_collision(): no collision, same-track collision, different-track collision,
                          multiple collisions
  - organise(): file moved, DB updated, sha256 from final path (P3, P5)
  - _refresh_plex(): correct URL constructed
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models import Base, Track, TrackStatus
from src.ingestion.organiser import FileOrganiser


# ── Fixtures ──────────────────────────────────────────────────────────────────

@pytest.fixture(scope="module")
def engine():
    eng = create_engine("sqlite:///:memory:", connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    return eng


@pytest.fixture
def session(engine):
    Session = sessionmaker(bind=engine, expire_on_commit=False)
    sess = Session()
    yield sess
    sess.rollback()
    sess.close()


def _make_organiser(media_drive="/media"):
    return FileOrganiser(
        media_drive=media_drive,
        plex_url="http://localhost:32400",
        plex_token="fake-token",
        plex_section_id="1",
    )


class _TrackStub:
    def __init__(self, **kwargs):
        defaults = dict(
            id=1,
            spotify_uri="spotify:track:stub",
            title="My Song",
            artist="The Artist",
            album_artist="The Artist",
            album="Great Album",
            year="2024",
            track_number=3,
            format="flac",
            status="pending",
            file_path=None,
            file_sha256=None,
            file_size_bytes=None,
        )
        defaults.update(kwargs)
        self.__dict__.update(defaults)


# ── _sanitize ─────────────────────────────────────────────────────────────────

class TestSanitize:
    def setup_method(self):
        self.org = _make_organiser()

    def test_replaces_all_forbidden_chars(self):
        raw = 'a<b>c:d"e/f\\g|h?i*j'
        assert self.org._sanitize(raw) == "a_b_c_d_e_f_g_h_i_j"

    def test_max_200_chars(self):
        assert len(self.org._sanitize("x" * 300)) == 200

    def test_strips_leading_trailing_periods_and_spaces(self):
        assert self.org._sanitize("  .hello.  ") == "hello"
        assert self.org._sanitize("...title...") == "title"

    def test_normal_name_unchanged(self):
        assert self.org._sanitize("Normal Name") == "Normal Name"

    def test_empty_string(self):
        assert self.org._sanitize("   ") == ""


# ── _build_path ───────────────────────────────────────────────────────────────

class TestBuildPath:
    def setup_method(self):
        self.org = _make_organiser()

    def test_full_path_with_year_and_track_number(self):
        t = _TrackStub(album_artist="Artist", album="Album", year="2024", track_number=3, title="Song")
        path = self.org._build_path(t, ".flac")
        assert path == os.path.join("/media", "Artist", "Album (2024)", "03 - Song.flac")

    def test_year_omitted_when_none(self):
        t = _TrackStub(year=None, track_number=1, title="Track")
        path = self.org._build_path(t, ".mp3")
        album_folder = Path(path).parent.name
        assert "(" not in album_folder

    def test_year_omitted_when_empty_string(self):
        t = _TrackStub(year="", track_number=1, title="Track")
        path = self.org._build_path(t, ".mp3")
        album_folder = Path(path).parent.name
        assert "(" not in album_folder

    def test_track_number_omitted_when_none(self):
        t = _TrackStub(track_number=None, title="Untitled")
        path = self.org._build_path(t, ".flac")
        assert os.path.basename(path) == "Untitled.flac"

    def test_track_number_zero_padded(self):
        t = _TrackStub(track_number=7, title="Lucky")
        path = self.org._build_path(t, ".flac")
        assert os.path.basename(path).startswith("07 - ")

    def test_album_artist_falls_back_to_artist(self):
        t = _TrackStub(album_artist=None, artist="Solo Artist")
        path = self.org._build_path(t, ".mp3")
        assert Path(path).parts[2] == "Solo Artist"

    def test_forbidden_chars_sanitized_in_all_components(self):
        t = _TrackStub(
            album_artist="AC/DC",
            album="Highway: To Hell",
            title="Back in Black",
            year="1980",
            track_number=1,
        )
        path = self.org._build_path(t, ".mp3")
        for part in Path(path).parts[1:]:
            assert "/" not in part
            assert ":" not in part


# ── _compute_sha256 ───────────────────────────────────────────────────────────

class TestComputeSha256:
    def setup_method(self):
        self.org = _make_organiser()

    def test_known_digest(self):
        data = b"hello world"
        expected = hashlib.sha256(data).hexdigest()
        with tempfile.NamedTemporaryFile(delete=False) as f:
            f.write(data)
            tmp = f.name
        try:
            assert self.org._compute_sha256(tmp) == expected
        finally:
            os.unlink(tmp)

    def test_empty_file(self):
        expected = hashlib.sha256(b"").hexdigest()
        with tempfile.NamedTemporaryFile(delete=False) as f:
            tmp = f.name
        try:
            assert self.org._compute_sha256(tmp) == expected
        finally:
            os.unlink(tmp)


# ── _resolve_collision ────────────────────────────────────────────────────────

class TestResolveCollision:
    def setup_method(self):
        self.org = _make_organiser()

    def _mock_session(self, existing: dict[str, int]):
        session = MagicMock()

        def _first():
            clause = session.query.return_value.filter.call_args[0][0]
            path_val = clause.right.value
            if path_val in existing:
                stub = MagicMock()
                stub.id = existing[path_val]
                return stub
            return None

        session.query.return_value.filter.return_value.first.side_effect = _first
        return session

    def test_no_collision_returns_original(self):
        base = os.path.join(os.sep + "media", "A", "B", "01 - Song.flac")
        result = self.org._resolve_collision(base, 1, self._mock_session({}))
        assert result == base

    def test_collision_different_track_appends_suffix(self):
        base = os.path.join(os.sep + "media", "A", "B", "01 - Song.flac")
        expected = os.path.join(os.sep + "media", "A", "B", "01 - Song (2).flac")
        result = self.org._resolve_collision(base, 1, self._mock_session({base: 99}))
        assert result == expected

    def test_collision_same_track_returns_original(self):
        base = os.path.join(os.sep + "media", "A", "B", "01 - Song.flac")
        result = self.org._resolve_collision(base, 1, self._mock_session({base: 1}))
        assert result == base

    def test_multiple_collisions_increments_counter(self):
        base = os.path.join(os.sep + "media", "A", "B", "01 - Song.flac")
        col2 = os.path.join(os.sep + "media", "A", "B", "01 - Song (2).flac")
        expected = os.path.join(os.sep + "media", "A", "B", "01 - Song (3).flac")
        result = self.org._resolve_collision(base, 1, self._mock_session({base: 99, col2: 100}))
        assert result == expected


# ── organise() integration ────────────────────────────────────────────────────

class TestOrganise:
    def test_file_moved_and_db_updated(self, session):
        """P3: downloaded track has non-null file_path and file_sha256."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a fake audio file in temp/
            src_file = os.path.join(tmpdir, "source.mp3")
            with open(src_file, "wb") as f:
                f.write(b"fake mp3 data")

            media_drive = os.path.join(tmpdir, "media")
            os.makedirs(media_drive, exist_ok=True)

            org = FileOrganiser(
                media_drive=media_drive,
                plex_url="http://localhost:32400",
                plex_token="tok",
                plex_section_id="1",
            )

            # Insert a real Track into the in-memory DB
            track = Track(
                spotify_uri="spotify:track:organise_test",
                title="Organise Song",
                artist="Artist",
                album_artist="Artist",
                album="Album",
                year="2024",
                track_number=1,
                status=TrackStatus.PENDING.value,
                claimed_at=datetime.now(timezone.utc),
                heartbeat_at=datetime.now(timezone.utc),
                claim_owner="worker:test",
                daemon_run_id=123,
                cover_art_source="none",
            )
            session.add(track)
            session.flush()

            with patch.object(org, "_refresh_plex"):
                final_path = org.organise(src_file, track, session)

            assert os.path.exists(final_path)
            assert track.status == TrackStatus.DOWNLOADED.value
            assert track.file_path == final_path
            assert track.file_sha256 is not None
            assert len(track.file_sha256) == 64  # SHA-256 hex digest
            assert track.file_size_bytes > 0
            assert track.format == "mp3"
            assert track.claimed_at is None
            assert track.heartbeat_at is None
            assert track.claim_owner is None
            assert track.daemon_run_id is None

    def test_sha256_computed_from_final_path_not_temp(self, session):
        """P3 correctness: SHA-256 must be of the file at its final path."""
        with tempfile.TemporaryDirectory() as tmpdir:
            src_file = os.path.join(tmpdir, "source.flac")
            content = b"flac audio content"
            with open(src_file, "wb") as f:
                f.write(content)

            expected_sha = hashlib.sha256(content).hexdigest()
            media_drive = os.path.join(tmpdir, "media")
            os.makedirs(media_drive, exist_ok=True)

            org = FileOrganiser(
                media_drive=media_drive,
                plex_url="http://localhost:32400",
                plex_token="tok",
                plex_section_id="1",
            )

            track = Track(
                spotify_uri="spotify:track:sha_test",
                title="SHA Test",
                artist="Artist",
                album_artist="Artist",
                album="Album",
                year="2024",
                track_number=2,
                status=TrackStatus.PENDING.value,
                cover_art_source="none",
            )
            session.add(track)
            session.flush()

            with patch.object(org, "_refresh_plex"):
                org.organise(src_file, track, session)

            assert track.file_sha256 == expected_sha

    def test_plex_refresh_failure_does_not_abort_organise(self, session):
        with tempfile.TemporaryDirectory() as tmpdir:
            src_file = os.path.join(tmpdir, "source.mp3")
            with open(src_file, "wb") as f:
                f.write(b"fake mp3 data")

            media_drive = os.path.join(tmpdir, "media")
            os.makedirs(media_drive, exist_ok=True)

            org = FileOrganiser(
                media_drive=media_drive,
                plex_url="http://localhost:32400",
                plex_token="tok",
                plex_section_id="1",
            )

            track = Track(
                spotify_uri="spotify:track:plex_refresh_failure_test",
                title="Plex Failure Song",
                artist="Artist",
                album_artist="Artist",
                album="Album",
                year="2024",
                track_number=3,
                status=TrackStatus.PENDING.value,
                cover_art_source="none",
            )
            session.add(track)
            session.flush()

            with patch.object(org, "_refresh_plex", side_effect=RuntimeError("plex down")):
                final_path = org.organise(src_file, track, session)

            assert os.path.exists(final_path)
            assert track.status == TrackStatus.DOWNLOADED.value


# ── _refresh_plex ─────────────────────────────────────────────────────────────

class TestRefreshPlex:
    def test_refresh_if_due_refreshes_first_move(self):
        org = _make_organiser()
        with patch.object(org, "_refresh_plex") as mock_refresh:
            org._refresh_plex_if_due(now=100.0)
        mock_refresh.assert_called_once_with()

    def test_refresh_if_due_batches_by_move_count(self):
        with patch.dict(
            os.environ,
            {
                "PLEX_REFRESH_INTERVAL_SECONDS": "3600",
                "PLEX_REFRESH_BATCH_SIZE": "3",
            },
        ):
            org = _make_organiser()

        with patch.object(org, "_refresh_plex") as mock_refresh:
            org._refresh_plex_if_due(now=100.0)
            org._refresh_plex_if_due(now=101.0)
            org._refresh_plex_if_due(now=102.0)
            org._refresh_plex_if_due(now=103.0)

        assert mock_refresh.call_count == 2

    def test_refresh_if_due_batches_by_interval(self):
        with patch.dict(
            os.environ,
            {
                "PLEX_REFRESH_INTERVAL_SECONDS": "10",
                "PLEX_REFRESH_BATCH_SIZE": "99",
            },
        ):
            org = _make_organiser()

        with patch.object(org, "_refresh_plex") as mock_refresh:
            org._refresh_plex_if_due(now=100.0)
            org._refresh_plex_if_due(now=105.0)
            org._refresh_plex_if_due(now=111.0)

        assert mock_refresh.call_count == 2

    def test_gets_correct_url(self):
        org = _make_organiser()
        with patch.object(org._http, "get") as mock_get:
            mock_get.return_value = MagicMock(ok=True, status_code=200)
            org._refresh_plex()
        call_url = mock_get.call_args[0][0]
        assert "library/sections/1/refresh" in call_url
        assert "localhost:32400" in call_url

    def test_includes_plex_token_in_session_headers(self):
        # SPEC §B15: X-Plex-Token is sent as a request HEADER on the session,
        # NOT a URL query param (avoids leaking the token in logs/proxies).
        org = _make_organiser()
        assert org._http.headers.get("X-Plex-Token") == "fake-token"

    def test_non_ok_response_does_not_raise(self):
        org = _make_organiser()
        with patch.object(org._http, "get") as mock_get:
            mock_get.return_value = MagicMock(ok=False, status_code=500, text="error")
            org._refresh_plex()  # must not raise
