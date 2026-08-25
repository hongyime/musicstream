"""Wave 3 discover-weekly tests (SPEC.md §W3 T21–T23).

All HTTP is faked via a stub session; resolution logic is exercised against
the in-memory SQLite fixture DB.
"""

from __future__ import annotations

import pytest

from src.discovery import discover_weekly as dw
from src.discovery.discover_weekly import DiscoverWeekly
from src.models import LbRecommendation, Track
from tests.conftest import _make_track


JSPF = {
    "title": "Weekly Exploration Sunday, 23 August 2026",
    "track": [
        {
            "identifier": ["https://musicbrainz.org/recording/11111111-1111-1111-1111-111111111111"],
            "title": "Known MBID Hit",
            "creator": "",
            "duration": 200000,
            "extension": {
                "https://musicbrainz.org/doc/jspf#playlist": {
                    "artists": [{"artist": {"name": "Alpha"}, "joinphrase": ""}],
                }
            },
        },
        {
            "identifier": ["https://musicbrainz.org/recording/22222222-2222-2222-2222-222222222222"],
            "title": "Only Fuzzy Match",
            "creator": "Bravo",
            "duration": 180000,
        },
        {
            "identifier": ["https://musicbrainz.org/recording/33333333-3333-3333-3333-333333333333"],
            "title": "Brand New Discovery",
            "creator": "Charlie",
            "duration": 195000,
        },
    ],
}

PLAYLIST_LIST = {
    "payload": {
        "playlists": [
            {
                "playlist": {"title": JSPF["title"]},
                "identifier": "https://api.listenbrainz.org/1/playlist/aaaa1111-1111-1111-1111-111111111111",
            },
            {"playlist": {"title": "Unrelated mixtape"}, "identifier": "x"},
        ]
    }
}


class _StubHTTP:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def get(self, url, timeout=None):
        self.calls.append(url)
        for pattern, payload in self.responses.items():
            if pattern in url:
                return _Resp(200, payload)
        return _Resp(404, {})


class _Resp:
    def __init__(self, status, body):
        self.status_code = status
        self._body = body

    def json(self):
        return self._body


@pytest.fixture()
def seeded(session):
    """Track that matches entry #1 by exact MBID (downloaded)."""
    t = _make_track(
        session,
        "spotify:track:dw1",
        status="downloaded",
        title="Known MBID Hit",
        artist="Alpha",
        duration_ms=201000,
        mb_recording_id="11111111-1111-1111-1111-111111111111",
        file_path="Y:/music/a.mp3",
    )
    return t


@pytest.fixture()
def dwe(session, seeded):
    http = _StubHTTP({
        "/playlists": PLAYLIST_LIST,
        "/playlist/aaaa1111": {"playlist": JSPF},
    })
    dwe_engine = DiscoverWeekly(username="qa_user", http_session=http)
    return dwe_engine


def test_find_weekly_playlists_filters_and_extracts_mbid(dwe):
    found = dwe.find_weekly_playlists()
    assert len(found) == 1
    assert found[0]["kind"] == "weekly_exploration"
    assert found[0]["mbid"] == "aaaa1111-1111-1111-1111-111111111111"


def test_run_resolves_queues_and_exports(session, tmp_path, monkeypatch, seeded):
    # Second library track matching entry #2 via artist+title+duration
    _make_track(
        session,
        "spotify:track:dw2",
        status="downloaded",
        title="Only Fuzzy Match",
        artist="Bravo",
        duration_ms=181000,
        file_path="Y:/music/b.mp3",
    )
    # Keep the synthetic-ingest path offline (no real MusicBrainz call)
    from src.discovery.listenbrainz import ListenBrainzDiscovery

    monkeypatch.setattr(ListenBrainzDiscovery, "_fetch_mb_metadata", lambda self, mbid: None)
    monkeypatch.setattr("src.core.config.PLAYLISTS_EXPORT_DIR", str(tmp_path))
    http = _StubHTTP({
        "/playlists": PLAYLIST_LIST,
        "/playlist/aaaa1111": {"playlist": JSPF},
    })
    dwe = DiscoverWeekly(username="qa_user", http_session=http)

    summary = dwe.run(session)

    pl = summary["playlists"][0]
    assert pl["entries"] == 3
    assert pl["resolved_local"] == 2          # exact MBID hit + fuzzy hit
    assert pl["queued_missing"] == 1          # synthetic track created

    # Synthetic track follows repo convention: uri `mb:{mbid}`, pending, MBID set
    synth = (
        session.query(Track)
        .filter(Track.mb_recording_id == "33333333-3333-3333-3333-333333333333")
        .first()
    )
    assert synth is not None
    assert synth.spotify_uri == "mb:33333333-3333-3333-3333-333333333333"
    assert synth.status == "pending"
    assert synth.title == "Brand New Discovery"

    rec = (
        session.query(LbRecommendation)
        .filter_by(recording_mbid="33333333-3333-3333-3333-333333333333")
        .first()
    )
    assert rec is not None and rec.kind == "weekly_exploration"

    m3u = summary["m3u_paths"][0]
    content = open(m3u, encoding="utf-8").read()
    assert content.startswith("#EXTM3U")


def test_fuzzy_match_requires_duration_within_5s(session, seeded):
    dwe = DiscoverWeekly(username="qa_user")
    hit = dwe._fuzzy_match(session, "known mbid hit", "ALPHA", 204_000)   # +3s OK
    assert hit is not None and hit.id == seeded.id

    miss = dwe._fuzzy_match(session, "known mbid hit", "Alpha", 250_000)  # +49s NO
    assert miss is None


def test_blocked_tracks_never_resolve(session, seeded):
    seeded.blocked = True
    session.flush()

    hit = session.query(Track).filter(Track.id == seeded.id).first()
    dwe = DiscoverWeekly(username="qa_user")
    resolved = dwe._resolve_local(
        session,
        "11111111-1111-1111-1111-111111111111",
        "Known MBID Hit", "Alpha", 201000,
    )
    assert resolved is None or resolved.blocked is False



