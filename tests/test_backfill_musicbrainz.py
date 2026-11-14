from types import SimpleNamespace

from src.ingestion.tagger import MBData
from src.scripts.backfill_musicbrainz import enrich_track


def test_enrich_track_persists_only_missing_musicbrainz_ids():
    track = SimpleNamespace(
        id=7,
        isrc=None,
        mb_recording_id=None,
        mb_release_id=None,
        status="downloaded",
        file_path="/media/Artist/Album/01 - Song.mp3",
    )
    limiter = SimpleNamespace(is_healthy=lambda service: True, record_success=lambda service: None)
    tagger = SimpleNamespace(
        _rl=limiter,
        _fetch_musicbrainz=lambda value: MBData(recording_id="recording-id", release_id="release-id"),
    )

    assert enrich_track(tagger, track, apply=True) == "enriched"
    assert track.mb_recording_id == "recording-id"
    assert track.mb_release_id == "release-id"
    assert track.status == "downloaded"
    assert track.file_path == "/media/Artist/Album/01 - Song.mp3"


def test_enrich_track_falls_back_to_search_for_an_invalid_isrc():
    track = SimpleNamespace(id=8, title="Song", artist="Artist", isrc="not-an-isrc", mb_recording_id=None, mb_release_id=None)
    limiter = SimpleNamespace(is_healthy=lambda service: True, record_success=lambda service: None)
    tagger = SimpleNamespace(
        _rl=limiter,
        _mb_search=lambda title, artist: MBData(recording_id="recording-id", release_id="release-id"),
    )

    assert enrich_track(tagger, track, apply=True) == "enriched"
    assert track.mb_recording_id == "recording-id"
    assert track.mb_release_id == "release-id"


def test_enrich_track_uses_musicbrainz_lookup_for_a_valid_isrc():
    track = SimpleNamespace(
        id=9,
        isrc="USRC17607839",
        mb_recording_id=None,
        mb_release_id=None,
    )
    limiter = SimpleNamespace(is_healthy=lambda service: True, record_success=lambda service: None)
    calls = []
    tagger = SimpleNamespace(
        _rl=limiter,
        _fetch_musicbrainz=lambda value: calls.append(value) or MBData(recording_id="recording-id"),
    )

    assert enrich_track(tagger, track, apply=True) == "enriched"
    assert calls == [track]
