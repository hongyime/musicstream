"""Wave 3 notifier + token early-warning tests (SPEC.md §W3 T17/T18, V12/V13)."""

from __future__ import annotations

import json

import pytest

from src.core import config


# ── T17: webhook notifier ─────────────────────────────────────────────────────

@pytest.fixture()
def fake_post(monkeypatch):
    calls = []

    class _Resp:
        def __init__(self, status):
            self.status_code = status

    def _post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json})
        return _Resp(calls[-1].get("_status", 200))

    monkeypatch.setattr("src.services.notify.requests.post", _post)
    return calls


def test_summary_sent_when_notify_on_all(monkeypatch, fake_post):
    monkeypatch.setattr(config, "WEBHOOK_URL", "http://hook.local/x")
    monkeypatch.setattr(config, "NOTIFY_ON", "all")
    from src.services.notify import notify_run_summary

    assert notify_run_summary(
        run_type="download", downloaded=5, failed=1, scraped=9, requeued=0, notes=None,
    ) is True
    assert len(fake_post) == 1
    content = fake_post[0]["json"]["content"]
    assert "download" in content and "5" in content


def test_summary_blocked_when_notify_on_failures(monkeypatch, fake_post):
    monkeypatch.setattr(config, "WEBHOOK_URL", "http://hook.local/x")
    monkeypatch.setattr(config, "NOTIFY_ON", "failures")
    from src.services.notify import notify_run_summary

    assert notify_run_summary(run_type="download") is False
    assert fake_post == []


def test_failure_alert_allowed_in_both_modes(monkeypatch, fake_post):
    monkeypatch.setattr(config, "WEBHOOK_URL", "http://hook.local/x")
    from src.services.notify import notify_failure

    monkeypatch.setattr(config, "NOTIFY_ON", "failures")
    assert notify_failure("Token expiring") is True

    monkeypatch.setattr(config, "NOTIFY_ON", "all")
    assert notify_failure("Corrupt file", detail="track 42") is True


def test_disabled_without_webhook_url(monkeypatch, fake_post):
    monkeypatch.setattr(config, "WEBHOOK_URL", None)
    monkeypatch.setattr(config, "NOTIFY_ON", "none")
    from src.services.notify import notify_failure, notify_run_summary

    assert notify_failure("x") is False
    assert notify_run_summary(run_type="scrape") is False
    assert fake_post == []


def test_retries_on_5xx_then_succeeds(monkeypatch, fake_post):
    monkeypatch.setattr(config, "WEBHOOK_URL", "http://hook.local/x")
    sleeps = []
    monkeypatch.setattr("src.services.notify.time.sleep", lambda s: sleeps.append(s))
    statuses = iter([500, 500, 200])

    def flaky(url, json=None, timeout=None):
        return type("R", (), {"status_code": next(statuses)})()

    monkeypatch.setattr("src.services.notify.requests.post", flaky)
    from src.services.notify import notify_failure

    assert notify_failure("flaky target") is True
    assert len(sleeps) == 2  # backoff between the three attempts


def test_never_raises_when_target_dead(monkeypatch, fake_post):
    monkeypatch.setattr(config, "WEBHOOK_URL", "http://hook.local/x")
    monkeypatch.setattr("src.services.notify.time.sleep", lambda s: None)

    def dead(url, json=None, timeout=None):
        raise ConnectionError("nope")

    monkeypatch.setattr("src.services.notify.requests.post", dead)
    from src.services.notify import notify_failure

    assert notify_failure("dead target") is False  # V12: no exception escapes


# ── T18: Spotify token freshness probe ────────────────────────────────────────

def _write_token(tmp_path, hours_left):
    import time
    path = tmp_path / "spotify_token.json"
    data = {"access_token": "x", "expires_at": time.time() + hours_left * 3600}
    path.write_text(json.dumps(data), encoding="utf-8")
    return str(path)


def test_token_freshness_reads_cache(tmp_path, monkeypatch):
    from src.ingestion.spotify_auth import token_freshness

    path = _write_token(tmp_path, 72)
    info = token_freshness(cache_path=path)
    assert info["present"] is True
    assert 71 < info["hours_left"] <= 72


def test_probe_flags_degraded_and_recovers_on_refresh(tmp_path):
    from src.ingestion.spotify_auth import probe_token

    path = _write_token(tmp_path, 10)  # < 48h default warn window
    refresh_calls = []

    def good_refresher():
        refresh_calls.append(1)
        import time as _t
        path_obj = __import__("pathlib").Path(path)
        data = json.loads(path_obj.read_text())
        data["expires_at"] = _t.time() + 3600 * 90
        path_obj.write_text(json.dumps(data))
        return True

    result = probe_token(cache_path=path, refresher=good_refresher)

    assert len(refresh_calls) == 1
    assert result["refreshed"] is True
    assert result["degraded"] is False


def test_probe_stays_degraded_when_refresh_fails(tmp_path):
    from src.ingestion.spotify_auth import probe_token

    path = _write_token(tmp_path, 5)

    def bad_refresher():
        raise RuntimeError("refresh endpoint down")

    result = probe_token(cache_path=path, refresher=bad_refresher)

    assert result["degraded"] is True
    assert result["refreshed"] is False


def test_probe_healthy_when_far_from_expiry(tmp_path):
    from src.ingestion.spotify_auth import probe_token

    path = _write_token(tmp_path, 24 * 30)

    def never():  # pragma: no cover
        raise AssertionError("refresher must not run for healthy tokens")

    result = probe_token(cache_path=path, refresher=never)
    assert result["degraded"] is False


def test_refresh_spotify_token_if_expired_uses_expired_threshold(monkeypatch):
    calls = []

    def fake_probe_token(refresher=None, max_age_hours=None):
        calls.append({"refresher": refresher, "max_age_hours": max_age_hours})
        assert refresher() is True
        return {"present": True, "hours_left": 1.0, "degraded": False, "refreshed": True}

    monkeypatch.setattr("src.ingestion.spotify_auth.probe_token", fake_probe_token)
    monkeypatch.setattr("src.core.tasks._default_token_refresher", lambda: True)
    from src.core.tasks import refresh_spotify_token_if_expired

    result = refresh_spotify_token_if_expired()

    assert result["refreshed"] is True
    assert calls[0]["max_age_hours"] == 0
    assert callable(calls[0]["refresher"])


def test_refresh_spotify_token_if_expired_returns_degraded_on_probe_error(monkeypatch):
    def broken_probe_token(refresher=None, max_age_hours=None):
        raise RuntimeError("cache unreadable")

    monkeypatch.setattr("src.ingestion.spotify_auth.probe_token", broken_probe_token)
    from src.core.tasks import refresh_spotify_token_if_expired

    result = refresh_spotify_token_if_expired()

    assert result == {"present": False, "hours_left": None, "degraded": True, "refreshed": False}


def test_spotify_incremental_sync_refreshes_before_client_init(monkeypatch):
    events = []

    class _Session:
        def __enter__(self):
            events.append("session")
            return object()

        def __exit__(self, exc_type, exc, tb):
            return False

    class _Scraper:
        def __init__(self, client_id):
            events.append("scraper")

        def incremental_sync(self, session):
            events.append("sync")
            return 0

    monkeypatch.setattr("src.utils.wait_for_internet", lambda: events.append("internet"))
    monkeypatch.setattr(
        "src.core.tasks.refresh_spotify_token_if_expired",
        lambda max_age_hours=0: events.append(("refresh", max_age_hours)) or {"degraded": False, "refreshed": False},
    )
    monkeypatch.setattr("src.db.get_session", lambda: _Session())
    monkeypatch.setattr("src.ingestion.scraper.SpotifyScraper", _Scraper)
    from src.core.tasks import spotify_incremental_sync

    spotify_incremental_sync()

    assert events == ["internet", ("refresh", 0.5), "scraper", "session", "sync"]


def test_spotify_incremental_sync_skips_when_another_spotify_task_is_active(monkeypatch):
    from src.core import tasks

    events = []
    assert tasks._SPOTIFY_TASK_LOCK.acquire(blocking=False) is True
    try:
        monkeypatch.setattr("src.utils.wait_for_internet", lambda: events.append("internet"))
        monkeypatch.setattr(
            "src.core.tasks.refresh_spotify_token_if_expired",
            lambda max_age_hours=0: events.append(("refresh", max_age_hours)),
        )

        tasks.spotify_incremental_sync()
    finally:
        tasks._SPOTIFY_TASK_LOCK.release()

    assert events == ["internet"]
