"""Tests for src/core/tasks.py reset/requeue helpers."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

# Mock heavy optional deps so importing tasks (which can pull in the pipeline)
# never fails in a bare environment. No-op where the deps are installed.
for _mod in ("yt_dlp", "spotipy", "spotipy.oauth2", "ytmusicapi", "spotdl"):
    sys.modules.setdefault(_mod, MagicMock())

from src.models import Track, TrackStatus  # noqa: E402
from src.core.tasks import reset_failed_tracks  # noqa: E402


def _track(session, uri, status, attempt_count):
    t = Track(
        spotify_uri=uri,
        spotify_id=uri.split(":")[-1],
        title="t",
        artist="a",
        album="al",
        status=status,
        attempt_count=attempt_count,
        last_attempt_at=datetime.now(timezone.utc),
    )
    session.add(t)
    session.flush()
    return t


class TestResetFailedTracks:
    """reset_failed_tracks must requeue failed-family tracks AND clear
    attempt_count, or _should_give_up re-fails them on the first tier miss."""

    def test_clears_attempt_count_on_failed(self, session):
        t = _track(session, "spotify:track:rf1", "failed", attempt_count=25)
        n = reset_failed_tracks(session)
        session.expire_all()
        rt = session.get(Track, t.id)
        assert n == 1
        assert rt.status == TrackStatus.PENDING.value
        assert (rt.attempt_count or 0) == 0
        assert rt.last_attempt_at is None

    def test_covers_failed_validation_and_timed_out(self, session):
        a = _track(session, "spotify:track:rf2", "failed_validation", attempt_count=30)
        b = _track(session, "spotify:track:rf3", "timed_out", attempt_count=40)
        n = reset_failed_tracks(session)
        session.expire_all()
        assert n == 2
        assert session.get(Track, a.id).status == TrackStatus.PENDING.value
        assert (session.get(Track, a.id).attempt_count or 0) == 0
        assert (session.get(Track, b.id).attempt_count or 0) == 0

    def test_leaves_pending_and_downloaded_untouched(self, session):
        p = _track(session, "spotify:track:rf4", "pending", attempt_count=3)
        d = _track(session, "spotify:track:rf5", "downloaded", attempt_count=7)
        reset_failed_tracks(session)
        session.expire_all()
        # neither status is in the reset filter, so attempt_count is preserved
        assert session.get(Track, p.id).attempt_count == 3
        assert session.get(Track, d.id).attempt_count == 7
        assert session.get(Track, d.id).status == "downloaded"
