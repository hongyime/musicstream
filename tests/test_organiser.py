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
"""
from __future__ import annotations

import hashlib
import os
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models import Base, Track, TrackStatus
from src.exceptions import OrganiserError
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
    return FileOrganiser(media_drive=media_drive)


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
    def test_reuses_matching_file_already_owned_by_track(self, session, tmp_path):
        content = b"already organised audio"
        org = FileOrganiser(str(tmp_path / "media"))
        track = Track(spotify_uri="spotify:track:owned_file", title="Owned Song",
                      artist="Artist", album_artist="Artist", album="Album",
                      year="2024", track_number=1, status=TrackStatus.PENDING.value,
                      file_path=None, file_sha256=None, cover_art_source="none")
        session.add(track)
        session.flush()
        destination = org._build_path(track, ".mp3")
        os.makedirs(os.path.dirname(destination))
        Path(destination).write_bytes(content)
        track.file_path = destination
        track.file_sha256 = hashlib.sha256(content).hexdigest()
        source = tmp_path / "source.mp3"
        source.write_bytes(b"new download")

        assert org.organise(str(source), track, session) == destination

        assert Path(destination).read_bytes() == content
        assert Path(source).exists()
        assert not list(Path(destination).parent.glob("*.orphan-*"))
        assert track.status == TrackStatus.DOWNLOADED.value

    def test_owned_file_checksum_mismatch_stops_without_moving_files(self, session, tmp_path):
        org = FileOrganiser(str(tmp_path / "media"))
        track = Track(spotify_uri="spotify:track:owned_mismatch", title="Owned Song",
                      artist="Artist", album_artist="Artist", album="Album",
                      year="2024", track_number=1, status=TrackStatus.PENDING.value,
                      file_path=None, file_sha256="0" * 64, cover_art_source="none")
        session.add(track)
        session.flush()
        destination = org._build_path(track, ".mp3")
        os.makedirs(os.path.dirname(destination))
        Path(destination).write_bytes(b"existing data")
        track.file_path = destination
        source = tmp_path / "source.mp3"
        source.write_bytes(b"new download")

        with pytest.raises(OrganiserError, match="checksum does not match"):
            org.organise(str(source), track, session)

        assert Path(destination).read_bytes() == b"existing data"
        assert source.read_bytes() == b"new download"
        assert not list(Path(destination).parent.glob("*.orphan-*"))

    def test_unowned_existing_file_is_still_quarantined(self, session, tmp_path):
        org = FileOrganiser(str(tmp_path / "media"))
        track = Track(spotify_uri="spotify:track:unowned_orphan", title="Orphan Song",
                      artist="Artist", album_artist="Artist", album="Album",
                      year="2024", track_number=1, status=TrackStatus.PENDING.value,
                      cover_art_source="none")
        session.add(track)
        session.flush()
        destination = org._build_path(track, ".mp3")
        os.makedirs(os.path.dirname(destination))
        Path(destination).write_bytes(b"old unowned file")
        source = tmp_path / "source.mp3"
        source.write_bytes(b"new download")

        assert org.organise(str(source), track, session) == destination

        quarantined = list(Path(destination).parent.glob("*.orphan-*"))
        assert len(quarantined) == 1
        assert quarantined[0].read_bytes() == b"old unowned file"
        assert Path(destination).read_bytes() == b"new download"

    def test_file_moved_and_db_updated(self, session):
        """P3: downloaded track has non-null file_path and file_sha256."""
        with tempfile.TemporaryDirectory() as tmpdir:
            # Create a fake audio file in temp/
            src_file = os.path.join(tmpdir, "source.mp3")
            with open(src_file, "wb") as f:
                f.write(b"fake mp3 data")

            media_drive = os.path.join(tmpdir, "media")
            os.makedirs(media_drive, exist_ok=True)

            org = FileOrganiser(media_drive=media_drive)

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

            org = FileOrganiser(media_drive=media_drive)

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

            org.organise(src_file, track, session)

            assert track.file_sha256 == expected_sha

    def test_intent_recovers_complete_move_and_requeues_missing_candidate(self, tmp_path, monkeypatch):
        from contextlib import contextmanager
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from src.core import tasks
        from src.models import Base, DownloadIntent

        engine = create_engine(f"sqlite:///{tmp_path / 'recovery.db'}")
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        with factory() as db:
            track = Track(spotify_uri="spotify:track:recovery", title="Recovery", artist="Artist")
            db.add(track)
            db.flush()
            payload = b"complete audio"
            target = tmp_path / "complete.mp3"
            target.write_bytes(payload)
            import hashlib
            db.add(DownloadIntent(track_id=track.id, staged_path=str(tmp_path / "staged.mp3"),
                                  target_path=str(target), expected_sha256=hashlib.sha256(payload).hexdigest(),
                                  format="mp3"))
            db.commit()

        @contextmanager
        def test_session():
            with factory() as db:
                yield db
                db.commit()

        import src.db
        monkeypatch.setattr(src.db, "get_session", test_session)
        assert tasks.reconcile_download_intents() == 1
        with factory() as db:
            assert db.query(Track).one().status == TrackStatus.DOWNLOADED.value
            assert db.query(DownloadIntent).count() == 0
        engine.dispose()

    def test_intent_quarantines_partial_move_without_deleting_candidate(self, tmp_path, monkeypatch):
        from contextlib import contextmanager
        import hashlib
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from src.core import tasks
        from src.models import Base, DownloadIntent
        import src.db

        engine = create_engine(f"sqlite:///{tmp_path / 'partial.db'}")
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        partial = tmp_path / "partial.mp3"
        partial.write_bytes(b"partial")
        with factory() as db:
            track = Track(spotify_uri="spotify:track:partial", title="Partial", artist="Artist",
                          status=TrackStatus.DOWNLOADING.value)
            db.add(track)
            db.flush()
            db.add(DownloadIntent(track_id=track.id, staged_path=str(tmp_path / "staged.mp3"),
                                  target_path=str(partial), expected_sha256=hashlib.sha256(b"complete").hexdigest(),
                                  format="mp3"))
            db.commit()

        @contextmanager
        def test_session():
            with factory() as db:
                yield db
                db.commit()

        monkeypatch.setattr(src.db, "get_session", test_session)
        assert tasks.reconcile_download_intents() == 0
        assert not partial.exists()
        quarantined = list(tmp_path.glob("partial.mp3.orphan-*"))
        assert len(quarantined) == 1 and quarantined[0].read_bytes() == b"partial"
        with factory() as db:
            assert db.query(Track).one().status == TrackStatus.PENDING.value
            assert db.query(DownloadIntent).count() == 0
        engine.dispose()

    def test_intent_before_move_requeues_and_preserves_staged_file(self, tmp_path, monkeypatch):
        from contextlib import contextmanager
        import hashlib
        from sqlalchemy import create_engine
        from sqlalchemy.orm import sessionmaker
        from src.core import tasks
        from src.models import Base, DownloadIntent
        import src.db

        engine = create_engine(f"sqlite:///{tmp_path / 'before-move.db'}")
        Base.metadata.create_all(engine)
        factory = sessionmaker(engine, expire_on_commit=False)
        staged = tmp_path / "staged.mp3"
        staged.write_bytes(b"staged candidate")
        with factory() as db:
            track = Track(spotify_uri="spotify:track:before-move", title="Before", artist="Artist",
                          status=TrackStatus.DOWNLOADING.value)
            db.add(track)
            db.flush()
            db.add(DownloadIntent(track_id=track.id, staged_path=str(staged),
                                  target_path=str(tmp_path / "not-yet-moved.mp3"),
                                  expected_sha256=hashlib.sha256(b"staged candidate").hexdigest(), format="mp3"))
            db.commit()

        @contextmanager
        def test_session():
            with factory() as db:
                yield db
                db.commit()

        monkeypatch.setattr(src.db, "get_session", test_session)
        assert tasks.reconcile_download_intents() == 0
        quarantined = list(tmp_path.glob("staged.mp3.orphan-*"))
        assert len(quarantined) == 1 and quarantined[0].read_bytes() == b"staged candidate"
        with factory() as db:
            assert db.query(Track).one().status == TrackStatus.PENDING.value
        engine.dispose()

