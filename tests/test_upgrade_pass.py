"""Wave 3 upgrade-pass tests (SPEC.md §W3 T20, invariant V11)."""

from __future__ import annotations

import pytest

from src.core import config
from src.core.tasks import upgrade_pass
from tests.conftest import _make_track


def _downloaded(session, uri, method, fmt="mp3", blocked=False):
    return _make_track(
        session,
        uri,
        status="downloaded",
        format=fmt,
        download_method=method,
        file_path=f"Y:/music/{uri}.mp3",
        blocked=blocked,
    )


def test_requeues_sub_cutoff_candidates(session):
    t = _downloaded(session, "spotify:track:w3up1", "ytdlp_ytm")

    count = upgrade_pass(session)
    session.flush()

    assert count == 1
    session.refresh(t)
    assert t.status == "pending"
    assert t.attempt_count == 0


def test_skips_premium_sources(session):
    _downloaded(session, "spotify:track:w3up2", "spotiflac_deezer")
    _downloaded(session, "spotify:track:w3up3", "librespot")

    assert upgrade_pass(session) == 0


def test_skips_blocked(session):
    _downloaded(session, "spotify:track:w3up4", "ytdlp_ytm", blocked=True)

    assert upgrade_pass(session) == 0


def test_noop_when_cutoff_is_flac(session, monkeypatch):
    monkeypatch.setattr(config, "QUALITY_CUTOFF", "flac")
    _downloaded(session, "spotify:track:w3up5", "ytdlp_ytm")

    assert upgrade_pass(session) == 0
