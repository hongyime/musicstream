"""Unit tests for DownloadOrchestrator._score_youtube_candidate (P2-2).

OFFICIAL_SOURCE_FILTER_V1 scoring decides which YouTube/YT-Music candidate to
download and rejects lyric/cover/8D/nightcore/etc. A false-negative here
silently starves tiers 2/4, so the heuristic deserves table-driven coverage.

These are PURE-function tests: the scorer only reads the class-level token lists
(_OFFICIAL_TITLE_TOKENS / _BAD_TITLE_TOKENS) plus the passed info/track, so we
skip the heavy __init__ (tagger/organiser/rate-limiter) via __new__ and use a
SimpleNamespace as a duck-typed Track (only .title/.artist/.duration_ms read).
"""
from types import SimpleNamespace

import pytest

from src.ingestion.downloader import DownloadOrchestrator


def _scorer() -> DownloadOrchestrator:
    return DownloadOrchestrator.__new__(DownloadOrchestrator)


def _track(title="Song Title", artist="The Artist", duration_ms=200_000):
    return SimpleNamespace(title=title, artist=artist, duration_ms=duration_ms)


def _info(title="Song Title", channel="", duration=200):
    return {"title": title, "channel": channel, "duration": duration}


# (id, info, track, predicate(score) -> bool)
CASES = [
    # ' - Topic' auto-generated master-recording channel → strong positive.
    ("topic_channel",
     _info(title="Song Title", channel="The Artist - Topic", duration=200),
     _track(),
     lambda s: s >= 200),
    # VEVO channel → positive.
    ("vevo_channel",
     _info(title="Song Title (Official Video)", channel="TheArtistVEVO", duration=200),
     _track(),
     lambda s: s >= 150),
    # 'official audio' marker with a matching duration → at least the +60 bump.
    ("official_audio_title",
     _info(title="Song Title (Official Audio)", channel="Some Channel", duration=200),
     _track(),
     lambda s: s >= 60),
    # Lyric video → bad token, no offsetting positives → negative (caller rejects).
    ("lyric_video_rejected",
     _info(title="Song Title (Lyric Video)", channel="Random Uploader", duration=200),
     _track(),
     lambda s: s < 0),
    # Bad token that ALSO appears in the real track title must NOT be penalised
    # (a song genuinely titled 'Cover'). Topic+artist+official keep it positive.
    ("bad_token_in_real_title_not_penalised",
     _info(title="Cover (Official Audio)", channel="The Artist - Topic", duration=240),
     _track(title="Cover", artist="The Artist", duration_ms=240_000),
     lambda s: s > 0),
    # Duration mismatch beyond tolerance → heavy penalty even from a Topic channel.
    ("duration_mismatch_hard_penalty",
     _info(title="Song Title", channel="The Artist - Topic", duration=400),
     _track(duration_ms=200_000),
     lambda s: s < 0),
    # Artist name embedded in the channel name → +80.
    ("artist_in_channel",
     _info(title="Song Title", channel="The Artist Official", duration=200),
     _track(artist="The Artist"),
     lambda s: s >= 80),
    # Nightcore junk → negative.
    ("nightcore_rejected",
     _info(title="Song Title (Nightcore)", channel="x", duration=200),
     _track(),
     lambda s: s < 0),
    # Missing duration on the candidate must not crash the duration penalty.
    ("no_duration_info_ok",
     _info(title="Song Title (Official Audio)", channel="The Artist - Topic", duration=None),
     _track(),
     lambda s: s >= 200),
]


@pytest.mark.parametrize("info,track,predicate", [(c[1], c[2], c[3]) for c in CASES],
                         ids=[c[0] for c in CASES])
def test_score_youtube_candidate(info, track, predicate):
    score = _scorer()._score_youtube_candidate(info, track)
    assert predicate(score), f"unexpected score {score}"


def test_official_outranks_lyric_video():
    """Relative ordering: an official-audio match must beat a lyric video."""
    s = _scorer()
    track = _track(title="Song Title", artist="The Artist", duration_ms=200_000)
    official = s._score_youtube_candidate(
        _info(title="Song Title (Official Audio)", channel="The Artist - Topic", duration=200), track)
    lyric = s._score_youtube_candidate(
        _info(title="Song Title (Lyric Video)", channel="Random", duration=200), track)
    assert official > lyric


def test_uploader_fallback_when_channel_absent():
    """channel falls back to uploader when 'channel' key is absent."""
    s = _scorer()
    info = {"title": "Song Title", "uploader": "The Artist - Topic", "duration": 200}
    assert s._score_youtube_candidate(info, _track()) >= 200
