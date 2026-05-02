"""
Tests for musicstream/discovery/listenbrainz.py

Covers:
  - _fetch_recommendations(): correct URL, params, response parsing
  - _ingest_recommendation(): inserts LbRecommendation + Track, status transitions
  - run(): backfill vs poll count selection, skip already-present MBIDs
  - Duplicate spotify_uri guard (mb:{mbid})
"""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from src.models import Base, LbRecommendation, Track, TrackStatus
from src.discovery.listenbrainz import ListenBrainzDiscovery, BACKFILL_COUNT, POLL_COUNT


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


def _make_discovery(**kwargs):
    d = ListenBrainzDiscovery(
        token="fake-token",
        username="testuser",
    )
    return d


def _make_rec(mbid="mbid-001", title="Song", artist="Artist"):
    return {
        "recording_mbid": mbid,
        "recording_name": title,
        "artist_name": artist,
        "score": 0.95,
    }


# ── Constants ─────────────────────────────────────────────────────────────────

class TestConstants:
    def test_backfill_count_is_200(self):
        assert BACKFILL_COUNT == 200

    def test_poll_count_is_100(self):
        assert POLL_COUNT == 100


# ── _fetch_recommendations ────────────────────────────────────────────────────

class TestFetchRecommendations:
    def test_uses_correct_url_and_params(self):
        d = _make_discovery()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "payload": {"mbids": [_make_rec()]}
        }
        with patch.object(d._lb_session, "get", return_value=mock_resp) as mock_get:
            d._rl.is_healthy = MagicMock(return_value=True)
            d._rl.record_success = MagicMock()
            recs = d._fetch_recommendations(100)

        call_args = mock_get.call_args
        url = call_args[0][0]
        params = call_args[1]["params"]
        assert "testuser" in url
        assert params["count"] == 100
        assert params["artist_type"] == "top"

    def test_parses_mbids_payload(self):
        d = _make_discovery()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "payload": {"mbids": [_make_rec("mbid-a"), _make_rec("mbid-b")]}
        }
        with patch.object(d._lb_session, "get", return_value=mock_resp):
            d._rl.is_healthy = MagicMock(return_value=True)
            d._rl.record_success = MagicMock()
            recs = d._fetch_recommendations(100)
        assert len(recs) == 2

    def test_parses_recordings_payload(self):
        d = _make_discovery()
        mock_resp = MagicMock()
        mock_resp.status_code = 200
        mock_resp.json.return_value = {
            "payload": {"recordings": [_make_rec("mbid-c")]}
        }
        with patch.object(d._lb_session, "get", return_value=mock_resp):
            d._rl.is_healthy = MagicMock(return_value=True)
            d._rl.record_success = MagicMock()
            recs = d._fetch_recommendations(100)
        assert len(recs) == 1

    def test_raises_on_http_error(self):
        from src.exceptions import ListenBrainzError
        d = _make_discovery()
        mock_resp = MagicMock()
        mock_resp.status_code = 500
        mock_resp.text = "Internal Server Error"
        with patch.object(d._lb_session, "get", return_value=mock_resp):
            d._rl.is_healthy = MagicMock(return_value=True)
            d._rl.record_failure = MagicMock()
            with pytest.raises(ListenBrainzError):
                d._fetch_recommendations(100)

    def test_raises_when_username_not_set(self):
        from src.exceptions import ListenBrainzError
        d = ListenBrainzDiscovery(token="tok", username="")
        with pytest.raises(ListenBrainzError, match="LISTENBRAINZ_USERNAME"):
            d._fetch_recommendations(100)


# ── _ingest_recommendation ────────────────────────────────────────────────────

class TestIngestRecommendation:
    def test_creates_lb_recommendation_and_track(self, session):
        d = _make_discovery()
        rec = _make_rec("mbid-ingest-001", "New Song", "New Artist")

        # Mock MB metadata fetch
        with patch.object(d, "_fetch_mb_metadata", return_value={
            "title": "New Song",
            "artist-credit": [{"name": "New Artist"}],
        }):
            result = d._ingest_recommendation(rec, session)

        assert result is True
        lb = session.query(LbRecommendation).filter_by(recording_mbid="mbid-ingest-001").first()
        assert lb is not None
        assert lb.status == "ingested"

        track = session.query(Track).filter_by(spotify_uri="mb:mbid-ingest-001").first()
        assert track is not None
        assert track.status == TrackStatus.PENDING.value
        assert track.mb_recording_id == "mbid-ingest-001"

    def test_synthetic_spotify_uri_format(self, session):
        d = _make_discovery()
        rec = _make_rec("mbid-uri-format", "Song", "Artist")
        with patch.object(d, "_fetch_mb_metadata", return_value={
            "title": "Song",
            "artist-credit": [{"name": "Artist"}],
        }):
            d._ingest_recommendation(rec, session)

        track = session.query(Track).filter_by(spotify_uri="mb:mbid-uri-format").first()
        assert track is not None
        assert track.spotify_uri.startswith("mb:")

    def test_missing_metadata_marks_failed(self, session):
        d = _make_discovery()
        rec = {"recording_mbid": "mbid-no-meta", "score": 0.5}
        with patch.object(d, "_fetch_mb_metadata", return_value=None):
            result = d._ingest_recommendation(rec, session)

        assert result is False
        lb = session.query(LbRecommendation).filter_by(recording_mbid="mbid-no-meta").first()
        assert lb is not None
        assert lb.status == "failed"

    def test_duplicate_track_not_created_twice(self, session):
        d = _make_discovery()
        mbid = "mbid-duplicate-guard"
        # Pre-insert the track
        existing = Track(
            spotify_uri=f"mb:{mbid}",
            title="Existing",
            artist="Artist",
            status=TrackStatus.PENDING.value,
            cover_art_source="none",
        )
        session.add(existing)
        session.flush()

        rec = _make_rec(mbid, "Existing", "Artist")
        with patch.object(d, "_fetch_mb_metadata", return_value={
            "title": "Existing",
            "artist-credit": [{"name": "Artist"}],
        }):
            result = d._ingest_recommendation(rec, session)

        # Returns False because track already existed (not a *new* track)
        assert result is False
        # But LbRecommendation should be linked
        lb = session.query(LbRecommendation).filter_by(recording_mbid=mbid).first()
        assert lb is not None
        assert lb.status == "ingested"


# ── run() — backfill vs poll ──────────────────────────────────────────────────

class TestRun:
    def test_backfill_when_table_empty(self, session):
        """Empty lb_recommendations table → fetch BACKFILL_COUNT (200)."""
        # Ensure table is empty
        session.query(LbRecommendation).delete()
        session.flush()

        d = _make_discovery()
        fetch_calls = []

        def _fake_fetch(count):
            fetch_calls.append(count)
            return []

        with patch.object(d, "_fetch_recommendations", side_effect=_fake_fetch):
            d.run(session)

        assert fetch_calls == [BACKFILL_COUNT]

    def test_poll_when_table_has_data(self, session):
        """Non-empty lb_recommendations table → fetch POLL_COUNT (100)."""
        # Ensure at least one row exists
        existing = LbRecommendation(
            recording_mbid="mbid-existing-for-poll",
            fetched_at=datetime.now(timezone.utc),
            status="ingested",
        )
        session.add(existing)
        session.flush()

        d = _make_discovery()
        fetch_calls = []

        def _fake_fetch(count):
            fetch_calls.append(count)
            return []

        with patch.object(d, "_fetch_recommendations", side_effect=_fake_fetch):
            d.run(session)

        assert fetch_calls == [POLL_COUNT]

    def test_already_present_mbid_skipped(self, session):
        """Already-present recording_mbid values must be skipped."""
        mbid = "mbid-already-present"
        lb = LbRecommendation(
            recording_mbid=mbid,
            fetched_at=datetime.now(timezone.utc),
            status="ingested",
        )
        session.add(lb)
        session.flush()

        d = _make_discovery()
        ingest_calls = []

        with patch.object(d, "_fetch_recommendations", return_value=[_make_rec(mbid)]), \
             patch.object(d, "_ingest_recommendation", side_effect=lambda r, s: ingest_calls.append(r)):
            d.run(session)

        # _ingest_recommendation should NOT have been called for the existing MBID
        assert not any(r.get("recording_mbid") == mbid for r in ingest_calls)
