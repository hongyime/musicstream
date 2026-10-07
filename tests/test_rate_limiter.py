"""
Tests for musicstream/rate_limiter.py

Covers:
  - ServiceRateLimiter: all 11 service configs present, jitter range,
    circuit breaker open/close, backoff capping, unknown service error
  - ExpiringResolutionCache: set/get, TTL expiry, len, invalidate, purge
  - MusicDownloadChaosMonkey: disabled by default, failure rate, stats
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from src.rate_limiter import (
    AdaptiveProviderLimiter,
    ExpiringResolutionCache,
    MusicDownloadChaosMonkey,
    ProviderLimitConfig,
    ServiceRateLimiter,
    classify_provider_failure,
    is_youtube_bot_challenge,
)


# ── ServiceRateLimiter ────────────────────────────────────────────────────────

class TestServiceRateLimiterConfigs:
    REQUIRED_SERVICES = {
        "spotify", "librespot", "spotiflac", "youtube", "ytmusicapi", "spotdl",
        "soundcloud", "musicbrainz", "acoustid", "listenbrainz", "coverart",
    }

    def test_all_services_configured(self):
        rl = ServiceRateLimiter()
        assert self.REQUIRED_SERVICES == set(rl.CONFIGS.keys())

    def test_musicbrainz_base_is_1s(self):
        assert ServiceRateLimiter.CONFIGS["musicbrainz"].base == 1.0

    def test_musicbrainz_concurrent_is_1(self):
        assert ServiceRateLimiter.CONFIGS["musicbrainz"].concurrent == 1

    def test_acoustid_base_is_0_5s(self):
        assert ServiceRateLimiter.CONFIGS["acoustid"].base == 0.5

    def test_spotify_max_is_3600(self):
        assert ServiceRateLimiter.CONFIGS["spotify"].max == 3600

    def test_circuit_breaker_threshold_is_5(self):
        assert ServiceRateLimiter.CIRCUIT_BREAKER_THRESHOLD == 5

    def test_circuit_breaker_cooldown_is_30_minutes(self):
        assert ServiceRateLimiter.CIRCUIT_BREAKER_COOLDOWN == 1800


class TestServiceRateLimiterJitter:
    def test_jitter_is_within_range(self):
        rl = ServiceRateLimiter()
        for _ in range(100):
            j = rl._jitter(10.0)
            assert 0.0 <= j <= 3.0, f"Jitter {j} out of [0, 3.0]"

    def test_jitter_zero_base_returns_zero(self):
        rl = ServiceRateLimiter()
        assert rl._jitter(0.0) == 0.0


class TestServiceRateLimiterCircuitBreaker:
    def test_service_starts_healthy(self):
        rl = ServiceRateLimiter()
        assert rl.is_healthy("spotify") is True

    def test_five_failures_opens_circuit(self):
        rl = ServiceRateLimiter()
        for _ in range(5):
            rl.record_failure("spotify")
        assert rl.is_healthy("spotify") is False

    def test_four_failures_does_not_open_circuit(self):
        rl = ServiceRateLimiter()
        for _ in range(4):
            rl.record_failure("spotify")
        assert rl.is_healthy("spotify") is True

    def test_success_resets_failure_counter(self):
        rl = ServiceRateLimiter()
        for _ in range(4):
            rl.record_failure("spotify")
        rl.record_success("spotify")
        # After reset, 4 more failures should not open circuit
        for _ in range(4):
            rl.record_failure("spotify")
        assert rl.is_healthy("spotify") is True

    def test_circuit_auto_recovers_after_cooldown(self):
        rl = ServiceRateLimiter()
        for _ in range(5):
            rl.record_failure("youtube")
        assert rl.is_healthy("youtube") is False

        # Simulate cooldown elapsed by patching monotonic
        with patch("time.monotonic", return_value=time.monotonic() + 1801):
            assert rl.is_healthy("youtube") is True

    def test_unknown_service_raises_value_error(self):
        rl = ServiceRateLimiter()
        with pytest.raises(ValueError, match="Unknown service"):
            rl.is_healthy("nonexistent_service")

    def test_record_failure_unknown_service_raises(self):
        rl = ServiceRateLimiter()
        with pytest.raises(ValueError):
            rl.record_failure("nonexistent")


class TestServiceRateLimiterWait:
    def test_wait_sleeps_positive_duration(self):
        rl = ServiceRateLimiter()
        sleep_calls = []
        with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            rl.wait("musicbrainz", attempt=0)
        assert len(sleep_calls) == 1
        assert sleep_calls[0] >= 1.0  # base is 1.0s

    def test_wait_respects_retry_after(self):
        rl = ServiceRateLimiter()
        sleep_calls = []
        with patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            rl.wait("spotify", attempt=0, retry_after=60.0)
        assert sleep_calls[0] >= 60.0

    def test_backoff_is_capped_at_max(self):
        rl = ServiceRateLimiter()
        # attempt=20 would give base * 2^20 >> max without capping
        computed = rl.calculate_wait_time("musicbrainz", attempt=20)
        assert computed <= ServiceRateLimiter.CONFIGS["musicbrainz"].max * 1.3  # allow jitter


class TestAdaptiveProviderLimiter:
    @staticmethod
    def _clear_youtube_env(monkeypatch):
        for suffix in ("MIN_GAP_S", "MAX_GAP_S", "MAX_INFLIGHT"):
            monkeypatch.delenv(f"PROVIDER_LIMITER_YOUTUBE_{suffix}", raising=False)

    def test_defaults_to_shadow_mode(self, monkeypatch):
        monkeypatch.delenv("PROVIDER_LIMITER_ENABLED", raising=False)
        limiter = AdaptiveProviderLimiter()
        assert limiter.snapshot()["enabled"] is False
        assert limiter.snapshot()["mode"] == "shadow"

    def test_youtube_challenge_is_a_throttle_signal_not_content_miss(self):
        from src.ingestion.downloader import DownloadOrchestrator

        challenge = Exception("Sign in to confirm you're not a bot")
        assert is_youtube_bot_challenge(challenge)
        assert classify_provider_failure(challenge) == "bot_challenge"
        assert DownloadOrchestrator._is_content_error(challenge) is False
        assert DownloadOrchestrator._is_content_error(Exception("Sign in to confirm your age")) is True

    def test_enforced_max_inflight_is_atomic_across_threads(self, monkeypatch):
        self._clear_youtube_env(monkeypatch)
        limiter = AdaptiveProviderLimiter(
            enabled=True,
            provider_configs={"youtube": ProviderLimitConfig(0, 0, 1)},
        )
        first = limiter.acquire("youtube")
        assert first is not None

        acquired = threading.Event()
        second_permit = []

        def worker():
            second_permit.append(limiter.acquire("youtube", wait_cap_s=1.0))
            acquired.set()

        thread = threading.Thread(target=worker)
        thread.start()
        assert acquired.wait(0.05) is False
        limiter.complete(first, success=True)
        assert acquired.wait(0.5) is True
        thread.join(timeout=0.5)
        assert second_permit[0] is not None
        limiter.complete(second_permit[0], success=True)
        assert limiter.snapshot()["providers"]["youtube"]["in_flight"] == 0

    def test_backoff_is_scoped_to_provider_and_caps_retry_after(self, monkeypatch):
        self._clear_youtube_env(monkeypatch)
        limiter = AdaptiveProviderLimiter(
            enabled=False,
            min_samples=1,
            signal_ratio=0.5,
            backoff_factor=2,
            retry_after_cap_s=20,
            provider_configs={"youtube": ProviderLimitConfig(1, 8, 1)},
        )
        permit = limiter.acquire("youtube")
        assert permit is not None
        limiter.complete(
            permit, success=False, failure_reason="bot_challenge", retry_after_s=100,
        )

        snapshot = limiter.snapshot()
        yt = snapshot["providers"]["youtube"]
        spotify = snapshot["providers"]["spotify_api"]
        assert yt["throttle_signal_count"] == 1
        assert yt["failure_reasons"] == {"bot_challenge": 1}
        assert yt["current_gap_s"] == 2
        assert yt["last_retry_after_s"] == 20
        assert yt["backoff_remaining_s"] > 0
        assert spotify["current_gap_s"] == spotify["min_gap_s"]
        assert spotify["backoff_remaining_s"] == 0

    def test_clean_traffic_recovers_gradually(self, monkeypatch):
        self._clear_youtube_env(monkeypatch)
        limiter = AdaptiveProviderLimiter(
            enabled=False,
            min_samples=1,
            signal_ratio=0.5,
            backoff_factor=2,
            recovery_factor=0.8,
            provider_configs={"youtube": ProviderLimitConfig(0.25, 8, 2)},
        )
        permit = limiter.acquire("youtube")
        assert permit is not None
        limiter.complete(permit, success=False, failure_reason="rate_limited")
        backed_off_gap = limiter.snapshot()["providers"]["youtube"]["current_gap_s"]
        assert backed_off_gap == 0.5

        clean_gaps = []
        for _ in range(3):
            permit = limiter.acquire("youtube")
            assert permit is not None
            limiter.complete(permit, success=True)
            clean_gaps.append(limiter.snapshot()["providers"]["youtube"]["current_gap_s"])
        assert backed_off_gap > clean_gaps[0] > clean_gaps[1] > clean_gaps[2]
        assert clean_gaps[-1] > 0.25

    def test_shadow_allows_would_be_throttled_call_and_enforce_skips_over_cap(self, monkeypatch):
        self._clear_youtube_env(monkeypatch)
        config = ProviderLimitConfig(1, 1, 1)
        shadow = AdaptiveProviderLimiter(
            enabled=False,
            provider_configs={"youtube": config},
        )
        first = shadow.acquire("youtube")
        assert first is not None
        shadow.complete(first, success=True)
        second = shadow.acquire("youtube")
        assert second is not None
        shadow.complete(second, success=True)
        shadow_metrics = shadow.snapshot()["providers"]["youtube"]
        assert shadow_metrics["would_throttle_count"] == 1
        assert shadow_metrics["request_count"] == 2

        enforcing = AdaptiveProviderLimiter(
            enabled=True,
            provider_configs={"youtube": config},
        )
        first = enforcing.acquire("youtube")
        assert first is not None
        enforcing.complete(first, success=True)
        assert enforcing.acquire("youtube", wait_cap_s=0.01) is None
        enforced_metrics = enforcing.snapshot()["providers"]["youtube"]
        assert enforced_metrics["skipped_count"] == 1
        assert enforced_metrics["request_count"] == 1


# ── ExpiringResolutionCache ───────────────────────────────────────────────────

class TestExpiringResolutionCache:
    def test_set_and_get(self):
        cache = ExpiringResolutionCache(default_ttl=60)
        cache.set("key1", "value1")
        assert cache.get("key1") == "value1"

    def test_get_missing_returns_default(self):
        cache = ExpiringResolutionCache()
        assert cache.get("missing") is None
        assert cache.get("missing", "fallback") == "fallback"

    def test_expired_entry_returns_none(self):
        cache = ExpiringResolutionCache(default_ttl=0.01)
        cache.set("expiring", "data")
        time.sleep(0.05)
        assert cache.get("expiring") is None

    def test_contains_true_for_live_entry(self):
        cache = ExpiringResolutionCache(default_ttl=60)
        cache["k"] = "v"
        assert "k" in cache

    def test_contains_false_for_expired(self):
        cache = ExpiringResolutionCache(default_ttl=0.01)
        cache["k"] = "v"
        time.sleep(0.05)
        assert "k" not in cache

    def test_len_counts_only_live_entries(self):
        cache = ExpiringResolutionCache(default_ttl=60)
        cache.set("a", 1)
        cache.set("b", 2)
        assert len(cache) == 2

    def test_invalidate_removes_entry(self):
        cache = ExpiringResolutionCache(default_ttl=60)
        cache.set("del_me", "x")
        cache.invalidate("del_me")
        assert cache.get("del_me") is None

    def test_clear_removes_all(self):
        cache = ExpiringResolutionCache(default_ttl=60)
        cache.set("a", 1)
        cache.set("b", 2)
        cache.clear()
        assert len(cache) == 0

    def test_purge_expired_returns_count(self):
        cache = ExpiringResolutionCache(default_ttl=0.01)
        cache.set("x", 1)
        cache.set("y", 2)
        time.sleep(0.05)
        removed = cache.purge_expired()
        assert removed == 2

    def test_per_entry_ttl_override(self):
        cache = ExpiringResolutionCache(default_ttl=60)
        cache.set("short", "v", ttl=0.01)
        time.sleep(0.05)
        assert cache.get("short") is None

    def test_getitem_raises_key_error_for_missing(self):
        cache = ExpiringResolutionCache()
        with pytest.raises(KeyError):
            _ = cache["nonexistent"]


# ── MusicDownloadChaosMonkey ──────────────────────────────────────────────────

class TestMusicDownloadChaosMonkey:
    def test_disabled_by_default_never_raises(self):
        monkey = MusicDownloadChaosMonkey(enabled=False)
        for _ in range(100):
            monkey.inject_chaos()  # must not raise

    def test_enabled_with_rate_1_always_raises(self):
        monkey = MusicDownloadChaosMonkey(enabled=True, failure_rate=1.0)
        with pytest.raises(RuntimeError):
            monkey.inject_chaos()

    def test_enabled_with_rate_0_never_raises(self):
        monkey = MusicDownloadChaosMonkey(enabled=True, failure_rate=0.0)
        for _ in range(50):
            monkey.inject_chaos()

    def test_stats_track_calls_and_injections(self):
        monkey = MusicDownloadChaosMonkey(enabled=True, failure_rate=1.0)
        for _ in range(5):
            try:
                monkey.inject_chaos()
            except RuntimeError:
                pass
        stats = monkey.stats
        assert stats["calls"] == 5
        assert stats["injections"] == 5

    def test_reset_stats_clears_counters(self):
        monkey = MusicDownloadChaosMonkey(enabled=True, failure_rate=1.0)
        try:
            monkey.inject_chaos()
        except RuntimeError:
            pass
        monkey.reset_stats()
        assert monkey.stats["calls"] == 0
        assert monkey.stats["injections"] == 0

    def test_invalid_failure_rate_raises(self):
        with pytest.raises(ValueError):
            MusicDownloadChaosMonkey(enabled=True, failure_rate=1.5)

    def test_intensity_presets(self):
        for intensity in ("low", "medium", "high"):
            m = MusicDownloadChaosMonkey(enabled=True, intensity=intensity)
            assert 0 < m.failure_rate <= 1.0

    def test_invalid_intensity_raises(self):
        with pytest.raises(ValueError):
            MusicDownloadChaosMonkey(enabled=True, intensity="extreme")

    def test_custom_exception_factory(self):
        monkey = MusicDownloadChaosMonkey(enabled=True, failure_rate=1.0)
        with pytest.raises(ValueError, match="custom"):
            monkey.inject_chaos(exception_factory=lambda: ValueError("custom"))
