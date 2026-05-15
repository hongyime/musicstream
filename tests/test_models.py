"""
Tests for musicstream/models.py

Verifies ORM model structure, enum values, defaults, and relationships
using an in-memory SQLite database (no PostgreSQL required).
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from src.models import (
    Base,
    DaemonRun,
    DownloadAttempt,
    LbRecommendation,
    Source,
    SourceType,
    Track,
    TrackStatus,
    track_sources,
)


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


def _utcnow():
    return datetime.now(timezone.utc)


def _make_track(session, **kwargs):
    defaults = dict(
        spotify_uri=f"spotify:track:{id(kwargs)}",
        spotify_id="abc123",
        title="Test Track",
        artist="Test Artist",
        status=TrackStatus.PENDING.value,
        cover_art_source="none",
    )
    defaults.update(kwargs)
    t = Track(**defaults)
    session.add(t)
    session.flush()
    return t


# ── TrackStatus enum ──────────────────────────────────────────────────────────

class TestTrackStatus:
    def test_all_required_values_exist(self):
        values = {s.value for s in TrackStatus}
        assert "pending" in values
        assert "resolving" in values
        assert "downloading" in values
        assert "downloaded" in values
        assert "failed" in values
        assert "failed_validation" in values
        assert "missing" in values

    def test_is_string_enum(self):
        assert isinstance(TrackStatus.PENDING, str)
        assert TrackStatus.PENDING == "pending"


class TestSourceType:
    def test_all_required_values_exist(self):
        values = {s.value for s in SourceType}
        assert "playlist" in values
        assert "liked" in values
        assert "listenbrainz" in values

    def test_is_string_enum(self):
        assert isinstance(SourceType.PLAYLIST, str)


# ── Track model ───────────────────────────────────────────────────────────────

class TestTrackModel:
    def test_required_columns_exist(self, engine):
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("tracks")}
        required = {
            "id", "spotify_uri", "spotify_id", "spotify_album_id", "isrc",
            "title", "artist", "album_artist", "album", "year",
            "track_number", "disc_number", "duration_ms",
            "cover_art_url", "cover_art_source",
            "mb_recording_id", "mb_release_id", "acoustid_id",
            "status", "download_method", "format",
            "file_path", "file_size_bytes", "file_sha256",
            "plex_verified", "created_at", "updated_at", "last_checked_at",
        }
        missing = required - cols
        assert not missing, f"Missing columns: {missing}"

    def test_spotify_uri_is_unique(self, session):
        uri = "spotify:track:unique_test_001"
        _make_track(session, spotify_uri=uri)
        with pytest.raises(Exception):
            _make_track(session, spotify_uri=uri)

    def test_default_status_is_pending(self, session):
        t = Track(
            spotify_uri="spotify:track:default_status_test",
            title="T",
            artist="A",
            cover_art_source="none",
        )
        session.add(t)
        session.flush()
        assert t.status == TrackStatus.PENDING.value

    def test_default_plex_verified_is_false(self, session):
        t = _make_track(session, spotify_uri="spotify:track:plex_test")
        assert t.plex_verified is False

    def test_default_cover_art_source_is_none(self, session):
        t = _make_track(session, spotify_uri="spotify:track:cover_test")
        assert t.cover_art_source == "none"

    def test_created_at_set_automatically(self, session):
        t = _make_track(session, spotify_uri="spotify:track:ts_test")
        assert t.created_at is not None

    def test_optional_fields_accept_none(self, session):
        t = _make_track(
            session,
            spotify_uri="spotify:track:optional_test",
            isrc=None,
            album=None,
            year=None,
            track_number=None,
            disc_number=None,
            file_path=None,
            file_sha256=None,
        )
        assert t.isrc is None
        assert t.file_path is None

    def test_repr_contains_key_fields(self, session):
        t = _make_track(session, spotify_uri="spotify:track:repr_test", title="Repr Song")
        r = repr(t)
        assert "repr_test" in r
        assert "Repr Song" in r


# ── Source model ──────────────────────────────────────────────────────────────

class TestSourceModel:
    def test_required_columns_exist(self, engine):
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("sources")}
        required = {"id", "spotify_id", "name", "source_type", "snapshot_id", "track_count", "last_scraped_at"}
        assert not (required - cols)

    def test_default_track_count_is_zero(self, session):
        src = Source(
            spotify_id="pl_default_count",
            name="Test Playlist",
            source_type=SourceType.PLAYLIST.value,
        )
        session.add(src)
        session.flush()
        assert src.track_count == 0

    def test_spotify_id_is_unique(self, session):
        src1 = Source(spotify_id="unique_src_001", name="P1", source_type="playlist")
        session.add(src1)
        session.flush()
        src2 = Source(spotify_id="unique_src_001", name="P2", source_type="playlist")
        session.add(src2)
        with pytest.raises(Exception):
            session.flush()


# ── track_sources association table ──────────────────────────────────────────

class TestTrackSourcesAssociation:
    def test_many_to_many_link(self, session):
        track = _make_track(session, spotify_uri="spotify:track:m2m_test")
        src = Source(
            spotify_id="m2m_playlist",
            name="M2M Playlist",
            source_type=SourceType.PLAYLIST.value,
        )
        session.add(src)
        session.flush()
        track.sources.append(src)
        session.flush()
        assert src in track.sources
        assert track in src.tracks


# ── LbRecommendation model ────────────────────────────────────────────────────

class TestLbRecommendationModel:
    def test_required_columns_exist(self, engine):
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("lb_recommendations")}
        required = {"id", "recording_mbid", "title", "artist", "score", "fetched_at", "track_id", "status"}
        assert not (required - cols)

    def test_default_status_is_pending(self, session):
        lb = LbRecommendation(
            recording_mbid="mbid-default-status",
            fetched_at=_utcnow(),
        )
        session.add(lb)
        session.flush()
        assert lb.status == "pending"

    def test_recording_mbid_is_unique(self, session):
        lb1 = LbRecommendation(recording_mbid="mbid-unique-001", fetched_at=_utcnow())
        session.add(lb1)
        session.flush()
        lb2 = LbRecommendation(recording_mbid="mbid-unique-001", fetched_at=_utcnow())
        session.add(lb2)
        with pytest.raises(Exception):
            session.flush()


# ── DownloadAttempt model ─────────────────────────────────────────────────────

class TestDownloadAttemptModel:
    def test_required_columns_exist(self, engine):
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("download_attempts")}
        required = {"id", "track_id", "attempted_at", "method", "error", "success"}
        assert not (required - cols)

    def test_default_success_is_false(self, session):
        track = _make_track(session, spotify_uri="spotify:track:attempt_test")
        attempt = DownloadAttempt(
            track_id=track.id,
            attempted_at=_utcnow(),
            method="tier2_ytdlp_ytm",
        )
        session.add(attempt)
        session.flush()
        assert attempt.success is False

    def test_cascade_delete_with_track(self, session):
        track = _make_track(session, spotify_uri="spotify:track:cascade_test")
        attempt = DownloadAttempt(
            track_id=track.id,
            attempted_at=_utcnow(),
            method="tier2_ytdlp_ytm",
        )
        session.add(attempt)
        session.flush()
        attempt_id = attempt.id
        session.delete(track)
        session.flush()
        result = session.get(DownloadAttempt, attempt_id)
        assert result is None


# ── DaemonRun model ───────────────────────────────────────────────────────────

class TestDaemonRunModel:
    def test_required_columns_exist(self, engine):
        inspector = inspect(engine)
        cols = {c["name"] for c in inspector.get_columns("daemon_runs")}
        required = {
            "id", "started_at", "completed_at", "run_type",
            "tracks_scraped", "tracks_downloaded", "tracks_failed", "tracks_requeued", "notes",
        }
        assert not (required - cols)

    def test_default_counters_are_zero(self, session):
        run = DaemonRun(started_at=_utcnow(), run_type="scheduled")
        session.add(run)
        session.flush()
        assert run.tracks_scraped == 0
        assert run.tracks_downloaded == 0
        assert run.tracks_failed == 0
        assert run.tracks_requeued == 0
