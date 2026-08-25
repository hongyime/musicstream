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
