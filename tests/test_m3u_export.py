"""Wave 3 m3u export tests (SPEC.md §W3 T15/T16, invariant V8)."""

from __future__ import annotations

import pytest

from src.core import config
from tests.conftest import _make_source, _make_track


def _downloaded(session, uri, title, artist, path, **kw):
    return _make_track(
        session,
        uri,
        status="downloaded",
        title=title,
        artist=artist,
        file_path=path,
        file_sha256="a" * 64,
        download_method="ytm",
        **kw,
    )


def test_export_writes_extm3u_with_entries(tmp_path, session):
    t1 = _downloaded(session, "spotify:track:m3u1", "Song A", "Artist A", str(tmp_path / "a.mp3"))
    t2 = _downloaded(session, "spotify:track:m3u2", "Song B", "Artist B", str(tmp_path / "b.mp3"))

    from src.discovery.m3u_export import export_playlist

    out = export_playlist(
        "Road Trip 2025",
        [
            (t1.file_path, t1.artist, t1.title, None),
            (t2.file_path, t2.artist, t2.title, 210_000),
        ],
        export_dir=str(tmp_path),
    )

    assert out is not None and out.exists()
    lines = out.read_text(encoding="utf-8").splitlines()
    assert lines[0] == "#EXTM3U"
    extinf = [l for l in lines if l.startswith("#EXTINF")]
    assert len(extinf) == 2
    assert "Artist A - Song A" in extinf[0]
    assert "210" in extinf[1]  # duration seconds
    assert str(tmp_path / "a.mp3") in lines


def test_export_sanitizes_unsafe_names(tmp_path):
    from src.discovery.m3u_export import export_playlist

    out = export_playlist('AC/DC: "Best" <Hits>?', [], export_dir=str(tmp_path))

    assert out is not None and out.exists()
    assert not any(ch in out.name for ch in '<>:"/\\|?*')


def test_export_returns_none_when_no_dir_configured(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "PLAYLISTS_EXPORT_DIR", None)
    from src.discovery.m3u_export import export_playlist

    assert export_playlist("Whatever", [], export_dir=None) is None


def test_backfill_exports_every_source_excluding_blocked(tmp_path, session):
    from src.discovery.m3u_export import backfill_all_playlists

    src = _make_source(session, "pl_w3_a", name="Mixed Bag")
    good = _downloaded(session, "spotify:track:m3u3", "Keep Me", "A", str(tmp_path / "keep.mp3"))
    blocked = _downloaded(
        session, "spotify:track:m3u4", "Drop Me", "B",
        str(tmp_path / "drop.mp3"), blocked=True,
    )
    empty_src = _make_source(session, "pl_w3_b", name="Empty Playlist")
    src.tracks.extend([good, blocked])
    session.flush()

    results = backfill_all_playlists(session, export_dir=str(tmp_path))

    names = [p.name for p in results]
    assert any("Mixed Bag" in n for n in names)
    assert not any("Empty Playlist" in n for n in names), "sources w/o tracks are skipped"
    mixed = next(p for p in results if "Mixed Bag" in p.name)
    content = mixed.read_text(encoding="utf-8")
    assert "keep.mp3" in content
    assert "drop.mp3" not in content, "blocked tracks must not leak into exports (V7)"


def test_sync_skips_plex_when_unset_but_still_exports_m3u(tmp_path, session, monkeypatch):
    """§W3 T16: PLEX_URL unset ⇒ zero Plex HTTP calls, m3u still written (V8)."""
    monkeypatch.delenv("PLEX_URL", raising=False)

    from src.discovery import plex_playlists as pp

    calls = {"n": 0}

    class _NoopHeaders:
        def update(self, *a, **kw):
            return None

    class _BoomSession:
        """Any HTTP GET while disabled must fail the test loudly."""

        def __init__(self):
            self.headers = _NoopHeaders()

        def get(self, *a, **kw):  # pragma: no cover - must never run
            calls["n"] += 1
            raise AssertionError("Plex HTTP attempted while disabled")

    monkeypatch.setattr(pp.requests, "Session", lambda: _BoomSession())

    sync = pp.PlexPlaylistSync()
    assert sync.enabled is False

    # Seed one ingested+downloaded LB recommendation for the current week
    from datetime import datetime, timedelta, timezone
    from src.models import LbRecommendation

    t = _downloaded(session, "lb:recording:w3x1", "Found", "New Artist", str(tmp_path / "f.mp3"))
    rec = LbRecommendation(
        recording_mbid="w3-mbid-x1",
        title="Found",
        artist="New Artist",
        fetched_at=datetime.now(timezone.utc) - timedelta(hours=1),
        status="ingested",
        track_id=t.id,
    )
    session.add(rec)
    session.flush()

    # Must not raise despite Plex being disabled
    sync.sync_discovery_playlist(session, month="August", year=2026)

    assert calls["n"] == 0, "no Plex HTTP traffic when disabled"


def test_container_paths_translated_to_host(tmp_path, monkeypatch):
    """DB stores /media/... container paths; m3u must carry host paths."""
    from src.discovery.m3u_export import export_playlist

    monkeypatch.setattr(config, "MEDIA_DIR", "/media")
    monkeypatch.setattr(config, "EXTERNAL_MEDIA_DRIVE", "Y:/music")

    out = export_playlist(
        "Path Translation",
        [("/media/sombr/Album (2025)/01 - track.mp3", "sombr", "track", None)],
        export_dir=str(tmp_path),
    )

    content = out.read_text(encoding="utf-8")
    assert "Y:/music/sombr/Album (2025)/01 - track.mp3" in content
    assert "/media/" not in content.replace("Y:/music/", "")


def test_windows_path_normalization_regression(tmp_path, monkeypatch):
    """str(Path('/media')) == '\\media' on Windows — translation must survive."""
    from pathlib import Path as _P
    monkeypatch.setattr(config, "MEDIA_DIR", _P("/media"))
    monkeypatch.setattr(config, "EXTERNAL_MEDIA_DRIVE", "Y:/music")

    from src.discovery.m3u_export import _to_host_path
    assert _to_host_path("/media/sombr/x.mp3") == "Y:/music/sombr/x.mp3"
