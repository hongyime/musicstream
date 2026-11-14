"""
rate_limiter.py — Per-service rate limiting with exponential backoff, jitter,
and a circuit breaker for the musicstream daemon.

Services covered (PRD §11):
  spotify, librespot, youtube, ytmusicapi, spotdl,
  musicbrainz, acoustid, listenbrainz, coverart, soundcloud
"""

from __future__ import annotations

import logging
import os
import random
import re
import threading
import time
from collections import Counter, deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Any, Callable, Deque, Dict, Optional

logger = logging.getLogger(__name__)
# ── Config dataclass ───────────────────────────────────────────────────────────

@dataclass(frozen=True)
class ServiceRateConfig:
    """Immutable rate-limit configuration for a single external service."""

    base: float       # Base backoff in seconds
    max: float        # Maximum backoff cap in seconds
    concurrent: int   # Maximum concurrent requests allowed


# ── Circuit-breaker state (mutable, per service) ───────────────────────────────

@dataclass
class _CircuitState:
    consecutive_failures: int = 0
    unhealthy_since: Optional[float] = None   # monotonic timestamp


# ── Main rate limiter ──────────────────────────────────────────────────────────

class ServiceRateLimiter:
    """
    Thread-safe per-service rate limiter with:
      - Exponential backoff + jitter on ``wait()``
      - Circuit breaker: 5 consecutive failures → unhealthy for 30 minutes
    """

    CONFIGS: Dict[str, ServiceRateConfig] = {
        "spotify":      ServiceRateConfig(base=3.0,  max=3600, concurrent=10),
        "librespot":    ServiceRateConfig(base=5.0,  max=120,  concurrent=1),   # Tier 0: direct Spotify CDN, serialised
        "spotiflac":    ServiceRateConfig(base=5.0,  max=300,  concurrent=2),   # Tier 1: lossless from other services
        "youtube":      ServiceRateConfig(base=4.0,  max=600,  concurrent=3),
        "ytmusicapi":   ServiceRateConfig(base=2.5,  max=300,  concurrent=5),
        "spotdl":       ServiceRateConfig(base=3.0,  max=180,  concurrent=3),
        "musicbrainz":  ServiceRateConfig(base=1.0,  max=60,   concurrent=1),
        "acoustid":     ServiceRateConfig(base=0.5,  max=30,   concurrent=3),
        "listenbrainz": ServiceRateConfig(base=1.0,  max=60,   concurrent=5),
        "coverart":     ServiceRateConfig(base=0.5,  max=30,   concurrent=5),
        "soundcloud":   ServiceRateConfig(base=2.0,  max=60,   concurrent=3),
    }

    CIRCUIT_BREAKER_THRESHOLD: int = 5       # default consecutive failures before unhealthy
    CIRCUIT_BREAKER_COOLDOWN: float = 1800   # default 30 minutes in seconds

    def __init__(
        self,
        circuit_breaker_threshold: int = 5,
        circuit_breaker_cooldown: float = 1800,
    ) -> None:
        self._lock = threading.Lock()
        # Allow per-instance override of class-level defaults
        self.CIRCUIT_BREAKER_THRESHOLD = circuit_breaker_threshold
        self.CIRCUIT_BREAKER_COOLDOWN = circuit_breaker_cooldown
        self._circuit: Dict[str, _CircuitState] = {
            svc: _CircuitState() for svc in self.CONFIGS
        }

    # ── Public API ─────────────────────────────────────────────────────────────

    def wait(self, service: str, attempt: int, retry_after: float = 0) -> None:
        """
        Sleep for the calculated backoff duration before the next request.

        If ``retry_after > 0`` (e.g. from a ``Retry-After`` HTTP header) that
        value is used directly (plus jitter).  Otherwise exponential backoff is
        applied: ``min(base * 2**attempt, max) + jitter``.

        Args:
            service:     Service key (must be in CONFIGS).
            attempt:     Zero-based retry attempt number.
            retry_after: Explicit wait time from the server (seconds).  0 means
                         use the computed backoff.
        """
        cfg = self._get_config(service)

        if retry_after > 0:
            backoff = retry_after + self._jitter(retry_after)
        else:
            raw = cfg.base * (2 ** attempt)
            capped = min(raw, cfg.max)
            backoff = capped + self._jitter(capped)

        logger.info(
            "Rate-limit wait: service=%s attempt=%d backoff=%.2fs",
            service, attempt, backoff,
        )
        time.sleep(backoff)

    def record_success(self, service: str) -> None:
        """Reset the consecutive-failure counter for *service*."""
        self._ensure_service(service)
        with self._lock:
            state = self._circuit[service]
            if state.consecutive_failures > 0:
                logger.debug(
                    "Circuit breaker reset: service=%s (was %d failures)",
                    service, state.consecutive_failures,
                )
            state.consecutive_failures = 0
            state.unhealthy_since = None

    def record_failure(self, service: str) -> None:
        """
        Increment the consecutive-failure counter for *service*.
        Marks the service as unhealthy once the threshold is reached.
        """
        self._ensure_service(service)
        with self._lock:
            state = self._circuit[service]
            state.consecutive_failures += 1
            logger.debug(
                "Failure recorded: service=%s consecutive=%d",
                service, state.consecutive_failures,
            )
            if (
                state.consecutive_failures >= self.CIRCUIT_BREAKER_THRESHOLD
                and state.unhealthy_since is None
            ):
                state.unhealthy_since = time.monotonic()
                logger.warning(
                    "Circuit breaker OPEN: service=%s will be skipped for %.0f minutes",
                    service, self.CIRCUIT_BREAKER_COOLDOWN / 60,
                )

    def force_open(self, service: str, reason: str = "") -> None:
        """Immediately open the circuit breaker for *service* regardless of failure count."""
        self._ensure_service(service)
        with self._lock:
            state = self._circuit[service]
            if state.unhealthy_since is None:
                state.consecutive_failures = self.CIRCUIT_BREAKER_THRESHOLD
                state.unhealthy_since = time.monotonic()
                logger.warning(
                    "Circuit breaker FORCE-OPEN: service=%s reason=%s cooldown=%.0fs",
                    service, reason or "forced", self.CIRCUIT_BREAKER_COOLDOWN,
                )

    def is_healthy(self, service: str) -> bool:
        """
        Return ``True`` if *service* is not currently in circuit-breaker cooldown.

        A service that was marked unhealthy automatically recovers after
        ``CIRCUIT_BREAKER_COOLDOWN`` seconds.
        """
        self._ensure_service(service)
        with self._lock:
            state = self._circuit[service]
            if state.unhealthy_since is None:
                return True
            elapsed = time.monotonic() - state.unhealthy_since
            if elapsed >= self.CIRCUIT_BREAKER_COOLDOWN:
                # Auto-recover: reset state so the service can be tried again
                logger.info(
                    "Circuit breaker CLOSED: service=%s recovered after %.0fs",
                    service, elapsed,
                )
                state.consecutive_failures = 0
                state.unhealthy_since = None
                return True
            remaining = self.CIRCUIT_BREAKER_COOLDOWN - elapsed
            logger.debug(
                "Circuit breaker still OPEN: service=%s %.0fs remaining",
                service, remaining,
            )
            return False

    # ── Internal helpers ───────────────────────────────────────────────────────

    def _jitter(self, base: float) -> float:
        """Return a random jitter value in ``[0, base * 0.3)``."""
        return random.uniform(0, base * 0.3)

    def _get_config(self, service: str) -> ServiceRateConfig:
        try:
            return self.CONFIGS[service]
        except KeyError:
            raise ValueError(
                f"Unknown service '{service}'. "
                f"Valid services: {sorted(self.CONFIGS)}"
            ) from None

    def _ensure_service(self, service: str) -> None:
        """Raise ValueError for unknown services; lazily init circuit state."""
        self._get_config(service)  # validates key
        with self._lock:
            if service not in self._circuit:
                self._circuit[service] = _CircuitState()

    # ── Legacy compatibility shims ─────────────────────────────────────────────
    # The old DualServiceRateLimiter used different method names.  These thin
    # wrappers allow existing call-sites (e.g. downloader.py) to keep working
    # until they are migrated to the new API.

    def begin_operation(self, service: str) -> None:
        """Legacy shim — no-op; concurrency tracking removed."""

    def end_operation(self, service: str) -> None:
        """Legacy shim — no-op; concurrency tracking removed."""

    def register_success(self, service: str) -> None:
        """Legacy shim → ``record_success``."""
        self.record_success(service)

    def register_failure(self, service: str) -> None:
        """Legacy shim → ``record_failure``."""
        self.record_failure(service)

    def calculate_wait_time(self, service: str, attempt: int) -> float:
        """
        Legacy shim — return the computed backoff without sleeping.
        Used by old call-sites that manage their own ``time.sleep``.
        """
        cfg = self._get_config(service)
        raw = cfg.base * (2 ** attempt)
        capped = min(raw, cfg.max)
        return capped + self._jitter(capped)


# ── Adaptive provider limiter and rolling metrics ────────────────────────────

@dataclass(frozen=True)
class ProviderLimitConfig:
    """Per-provider spacing and concurrency limits."""

    min_gap_s: float
    max_gap_s: float
    max_inflight: int


@dataclass
class _ProviderEvent:
    event_id: int
    started_at: float
    outcome: Optional[str] = None
    failure_reason: Optional[str] = None
    throttle_signal: bool = False


@dataclass
class _ProviderState:
    config: ProviderLimitConfig
    current_gap_s: float
    next_allowed_at: float = 0.0
    retry_after_until: float = 0.0
    last_retry_after_s: float = 0.0
    in_flight: int = 0
    skipped_count: int = 0
    would_throttle_count: int = 0
    next_event_id: int = 1
    last_backoff_signal_id: int = 0
    events: Deque[_ProviderEvent] = field(default_factory=deque)


@dataclass
class ProviderPermit:
    """A reserved provider operation. Complete it exactly once."""

    provider: str
    event: _ProviderEvent
    _finished: bool = False


_DEFAULT_PROVIDER_CONFIGS: Dict[str, ProviderLimitConfig] = {
    "youtube": ProviderLimitConfig(min_gap_s=4.5, max_gap_s=60.0, max_inflight=2),
    "spotify_api": ProviderLimitConfig(min_gap_s=0.1, max_gap_s=15.0, max_inflight=3),
    "spotify_streaming": ProviderLimitConfig(min_gap_s=0.0, max_gap_s=30.0, max_inflight=1),
    "spotiflac": ProviderLimitConfig(min_gap_s=5.0, max_gap_s=60.0, max_inflight=2),
    "spotdl": ProviderLimitConfig(min_gap_s=4.5, max_gap_s=60.0, max_inflight=3),
    "soundcloud": ProviderLimitConfig(min_gap_s=1.5, max_gap_s=30.0, max_inflight=2),
    "listenbrainz": ProviderLimitConfig(min_gap_s=1.0, max_gap_s=30.0, max_inflight=5),
    "musicbrainz": ProviderLimitConfig(min_gap_s=1.0, max_gap_s=15.0, max_inflight=1),
    "acoustid": ProviderLimitConfig(min_gap_s=0.5, max_gap_s=15.0, max_inflight=3),
    "artwork": ProviderLimitConfig(min_gap_s=0.5, max_gap_s=30.0, max_inflight=3),
}

_THROTTLE_FAILURE_REASONS = {"bot_challenge", "rate_limited", "http_429", "retry_after"}
_ORDINARY_MISS_REASONS = {"content_miss", "no_candidates", "not_found", "http_404"}


def is_youtube_bot_challenge(exc: BaseException | str) -> bool:
    """Identify YouTube's sign-in bot challenge separately from content misses."""
    message = str(exc).lower().replace("’", "'")
    return any(phrase in message for phrase in (
        "sign in to confirm you're not a bot",
        "sign in to confirm you are not a bot",
        "confirm you're not a bot",
        "confirm you are not a bot",
        "confirm that you're not a bot",
        "confirm that you are not a bot",
    ))


def extract_retry_after(exc: BaseException | Any, cap_s: Optional[float] = None) -> float:
    """Read numeric or HTTP-date Retry-After values from exceptions/responses."""
    candidates = [exc, getattr(exc, "response", None)]
    headers = getattr(exc, "headers", None)
    if headers is not None:
        candidates.append(headers)
    for candidate in candidates:
        if candidate is None:
            continue
        source = getattr(candidate, "headers", candidate)
        try:
            value = source.get("Retry-After") or source.get("retry-after")
        except (AttributeError, TypeError):
            value = None
        if value is None:
            continue
        try:
            seconds = max(0.0, float(value))
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(str(value))
                if retry_at.tzinfo is None:
                    retry_at = retry_at.replace(tzinfo=timezone.utc)
                seconds = max(0.0, (retry_at - datetime.now(timezone.utc)).total_seconds())
            except (TypeError, ValueError, OverflowError):
                continue
        return min(seconds, cap_s) if cap_s is not None else seconds
    match = re.search(r"retry[- ]after\s*[:=]\s*([0-9]+(?:\.[0-9]+)?)", str(exc), re.I)
    if match:
        seconds = float(match.group(1))
        return min(seconds, cap_s) if cap_s is not None else seconds
    return 0.0


def classify_provider_failure(exc: BaseException | Any) -> str:
    """Return a stable provider failure category for rolling metrics."""
    if is_youtube_bot_challenge(exc):
        return "bot_challenge"
    status = getattr(exc, "status_code", None) or getattr(exc, "http_status", None)
    response = getattr(exc, "response", None)
    if status is None and response is not None:
        status = getattr(response, "status_code", None)
    text = str(exc).lower()
    if status == 429 or any(token in text for token in (
        "too many requests", "http error 429", "http status 429", "rate limit", "rate-limited",
        "rate limited", "ratelimit", "retry will occur",
    )):
        return "rate_limited"
    if status in (401, 403) or any(token in text for token in (
        "unauthorized", "invalid token", "authentication failed", "invalid credentials",
    )):
        return "auth_failure"
    if status == 404 or any(token in text for token in (
        "video unavailable", "private video", "requested format is not available",
        "no results", "not found",
    )):
        return "content_miss"
    if "timeout" in text or "timed out" in text:
        return "timeout"
    return "provider_error"


class AdaptiveProviderLimiter:
    """Atomic, provider-scoped adaptive limiter with rolling outcome metrics.

    A disabled limiter still records requests and logs would-throttle events.
    ``observe_call`` is the integration stub used by non-YouTube providers; it
    gathers metrics without enforcing spacing or concurrency in this pass.
    """

    def __init__(
        self,
        *,
        enabled: Optional[bool] = None,
        window_s: Optional[float] = None,
        signal_ratio: Optional[float] = None,
        min_samples: Optional[int] = None,
        backoff_factor: Optional[float] = None,
        recovery_factor: Optional[float] = None,
        retry_after_cap_s: Optional[float] = None,
        provider_configs: Optional[Dict[str, ProviderLimitConfig | Dict[str, Any]]] = None,
    ) -> None:
        self.enabled = _env_bool("PROVIDER_LIMITER_ENABLED", False) if enabled is None else bool(enabled)
        self.window_s = _env_float("PROVIDER_LIMITER_WINDOW_S", 300.0) if window_s is None else float(window_s)
        self.signal_ratio = _env_float("PROVIDER_LIMITER_SIGNAL_RATIO", 0.2) if signal_ratio is None else float(signal_ratio)
        self.min_samples = _env_int("PROVIDER_LIMITER_MIN_SAMPLES", 10) if min_samples is None else int(min_samples)
        self.backoff_factor = _env_float("PROVIDER_LIMITER_BACKOFF_FACTOR", 2.0) if backoff_factor is None else float(backoff_factor)
        self.recovery_factor = _env_float("PROVIDER_LIMITER_RECOVERY_FACTOR", 0.9) if recovery_factor is None else float(recovery_factor)
        self.retry_after_cap_s = _env_float("PROVIDER_LIMITER_RETRY_AFTER_CAP_S", 300.0) if retry_after_cap_s is None else float(retry_after_cap_s)
        self._validate_global_config()

        self._configs = dict(_DEFAULT_PROVIDER_CONFIGS)
        if provider_configs:
            for provider, config in provider_configs.items():
                if provider not in self._configs:
                    raise ValueError(f"Unknown provider '{provider}'. Valid providers: {sorted(self._configs)}")
                if isinstance(config, ProviderLimitConfig):
                    self._configs[provider] = config
                else:
                    base = self._configs[provider]
                    self._configs[provider] = ProviderLimitConfig(
                        min_gap_s=float(config.get("min_gap_s", base.min_gap_s)),
                        max_gap_s=float(config.get("max_gap_s", base.max_gap_s)),
                        max_inflight=int(config.get("max_inflight", base.max_inflight)),
                    )
        self._configs = {provider: self._env_config(provider, config) for provider, config in self._configs.items()}
        for provider, config in self._configs.items():
            if config.min_gap_s < 0 or config.max_gap_s < config.min_gap_s or config.max_inflight < 1:
                raise ValueError(f"Invalid rate-limit config for {provider}: {config}")

        self._condition = threading.Condition(threading.RLock())
        self._states = {
            provider: _ProviderState(config=config, current_gap_s=config.min_gap_s)
            for provider, config in self._configs.items()
        }
        logger.info("Adaptive provider limiter mode=%s window=%.0fs", "enforce" if self.enabled else "shadow", self.window_s)

    def _validate_global_config(self) -> None:
        if self.window_s <= 0 or not 0 <= self.signal_ratio <= 1 or self.min_samples < 1:
            raise ValueError("Invalid provider limiter window, signal ratio, or minimum sample count")
        if self.backoff_factor <= 1 or not 0 < self.recovery_factor < 1 or self.retry_after_cap_s < 0:
            raise ValueError("Invalid provider limiter backoff, recovery, or Retry-After cap")

    @staticmethod
    def _env_config(provider: str, config: ProviderLimitConfig) -> ProviderLimitConfig:
        key = provider.upper()
        return ProviderLimitConfig(
            min_gap_s=_env_float(f"PROVIDER_LIMITER_{key}_MIN_GAP_S", config.min_gap_s),
            max_gap_s=_env_float(f"PROVIDER_LIMITER_{key}_MAX_GAP_S", config.max_gap_s),
            max_inflight=_env_int(f"PROVIDER_LIMITER_{key}_MAX_INFLIGHT", config.max_inflight),
        )

    def _prune(self, state: _ProviderState, now: float) -> None:
        cutoff = now - self.window_s
        while state.events and state.events[0].started_at < cutoff:
            state.events.popleft()

    def _metrics(self, provider: str, state: _ProviderState, now: float) -> dict[str, Any]:
        self._prune(state, now)
        events = list(state.events)
        requests = len(events)
        signals = sum(1 for event in events if event.throttle_signal)
        failure_reasons = Counter(
            event.failure_reason for event in events
            if event.outcome == "failure" and event.failure_reason
        )
        failures = sum(1 for event in events if event.outcome == "failure")
        successes = sum(1 for event in events if event.outcome == "success")
        return {
            "request_count": requests,
            "success_count": successes,
            "failure_count": failures,
            "throttle_signal_count": signals,
            "ordinary_miss_count": sum(failure_reasons.get(reason, 0) for reason in _ORDINARY_MISS_REASONS),
            "failure_reasons": dict(failure_reasons),
            "signal_ratio": round(signals / requests, 4) if requests else 0.0,
            "current_gap_s": round(state.current_gap_s, 3),
            "min_gap_s": state.config.min_gap_s,
            "max_gap_s": state.config.max_gap_s,
            "max_inflight": state.config.max_inflight,
            "in_flight": state.in_flight,
            "backoff_remaining_s": round(max(0.0, state.retry_after_until - now), 3),
            "last_retry_after_s": round(state.last_retry_after_s, 3),
            "skipped_count": state.skipped_count,
            "would_throttle_count": state.would_throttle_count,
        }

    def _new_event(self, provider: str, state: _ProviderState, now: float) -> _ProviderEvent:
        event = _ProviderEvent(event_id=state.next_event_id, started_at=now)
        state.next_event_id += 1
        state.events.append(event)
        self._prune(state, now)
        return event

    def acquire(
        self,
        provider: str,
        *,
        wait_cap_s: Optional[float] = None,
        enforce: Optional[bool] = None,
    ) -> Optional[ProviderPermit]:
        """Reserve an atomic provider slot, or return None when it should skip."""
        if provider not in self._states:
            raise ValueError(f"Unknown provider '{provider}'. Valid providers: {sorted(self._states)}")
        enforce = self.enabled if enforce is None else bool(enforce)
        state = self._states[provider]
        cap = min(state.config.max_gap_s, self.retry_after_cap_s) if wait_cap_s is None else max(0.0, float(wait_cap_s))
        deadline = time.monotonic() + cap

        with self._condition:
            while True:
                now = time.monotonic()
                self._prune(state, now)
                due_at = max(state.next_allowed_at, state.retry_after_until)
                wait_s = max(0.0, due_at - now)
                at_capacity = state.in_flight >= state.config.max_inflight

                if not enforce:
                    reserved_at = max(now, due_at)
                    if wait_s > 0 or at_capacity:
                        state.would_throttle_count += 1
                        logger.info(
                            "Provider limiter shadow: provider=%s wait=%.2fs in_flight=%d/%d cap=%.2fs",
                            provider, wait_s, state.in_flight, state.config.max_inflight, cap,
                        )
                    state.next_allowed_at = reserved_at + state.current_gap_s
                    event = self._new_event(provider, state, now)
                    state.in_flight += 1
                    return ProviderPermit(provider=provider, event=event)

                if not at_capacity and wait_s <= 0:
                    event = self._new_event(provider, state, now)
                    state.next_allowed_at = now + state.current_gap_s
                    state.in_flight += 1
                    return ProviderPermit(provider=provider, event=event)

                if wait_s > cap or now >= deadline:
                    state.skipped_count += 1
                    logger.info(
                        "Provider limiter skip: provider=%s wait=%.2fs in_flight=%d/%d cap=%.2fs",
                        provider, wait_s, state.in_flight, state.config.max_inflight, cap,
                    )
                    return None

                remaining = max(0.0, deadline - now)
                timeout = min(wait_s, remaining) if wait_s > 0 else remaining
                self._condition.wait(timeout=max(0.001, timeout))

    def complete(
        self,
        permit: ProviderPermit,
        *,
        success: bool,
        failure_reason: Optional[str] = None,
        retry_after_s: float = 0.0,
    ) -> None:
        """Record an operation result and release its in-flight slot."""
        if permit._finished:
            return
        with self._condition:
            if permit._finished:
                return
            state = self._states[permit.provider]
            now = time.monotonic()
            reason = None if success else (failure_reason or "provider_error")
            is_signal = bool(reason in _THROTTLE_FAILURE_REASONS or (retry_after_s and retry_after_s > 0))
            permit.event.outcome = "success" if success else "failure"
            permit.event.failure_reason = reason
            permit.event.throttle_signal = is_signal
            state.in_flight = max(0, state.in_flight - 1)

            if success:
                state.current_gap_s = max(state.config.min_gap_s, state.current_gap_s * self.recovery_factor)

            retry_after_s = min(max(0.0, float(retry_after_s or 0.0)), self.retry_after_cap_s)
            if retry_after_s:
                state.last_retry_after_s = retry_after_s
                state.retry_after_until = max(state.retry_after_until, now + retry_after_s)

            metrics = self._metrics(permit.provider, state, now)
            newest_signal_id = max(
                (event.event_id for event in state.events if event.throttle_signal),
                default=0,
            )
            pressure = (
                metrics["request_count"] >= self.min_samples
                and metrics["signal_ratio"] >= self.signal_ratio
            )
            if pressure and newest_signal_id > state.last_backoff_signal_id:
                old_gap = state.current_gap_s
                state.current_gap_s = min(state.config.max_gap_s, max(
                    state.config.min_gap_s, state.current_gap_s * self.backoff_factor,
                ))
                state.last_backoff_signal_id = newest_signal_id
                logger.warning(
                    "Adaptive provider backoff: provider=%s gap=%.2fs->%.2fs signal_ratio=%.3f samples=%d",
                    permit.provider, old_gap, state.current_gap_s,
                    metrics["signal_ratio"], metrics["request_count"],
                )
            if is_signal or retry_after_s:
                logger.warning(
                    "Provider throttle signal: provider=%s reason=%s retry_after=%.2fs",
                    permit.provider, reason or "retry_after", retry_after_s,
                )
            permit._finished = True
            self._condition.notify_all()

    def observe_call(
        self,
        provider: str,
        callback: Callable[..., Any],
        *args: Any,
        enforce: bool = False,
        **kwargs: Any,
    ) -> Any:
        """Call a provider while recording its outcome, optionally enforcing its limits."""
        permit = self.acquire(provider, enforce=enforce)
        assert permit is not None
        try:
            result = callback(*args, **kwargs)
        except Exception as exc:
            self.complete(
                permit, success=False, failure_reason=classify_provider_failure(exc),
                retry_after_s=extract_retry_after(exc, self.retry_after_cap_s),
            )
            raise
        status = getattr(result, "status_code", None)
        if isinstance(status, int) and status >= 400:
            reason = "rate_limited" if status == 429 else "auth_failure" if status in (401, 403) else "content_miss" if status == 404 else "http_error"
            self.complete(
                permit, success=False, failure_reason=reason,
                retry_after_s=extract_retry_after(result, self.retry_after_cap_s),
            )
        else:
            self.complete(permit, success=True)
        return result

    def snapshot(self) -> dict[str, Any]:
        """Return JSON-safe rolling metrics for all providers."""
        with self._condition:
            now = time.monotonic()
            providers = {
                provider: self._metrics(provider, state, now)
                for provider, state in self._states.items()
            }
        return {
            "enabled": self.enabled,
            "mode": "enforce" if self.enabled else "shadow",
            "window_s": self.window_s,
            "signal_ratio_threshold": self.signal_ratio,
            "min_samples": self.min_samples,
            "providers": providers,
        }

    def compact_snapshot(self) -> dict[str, Any]:
        """Small health payload showing only active or recently used providers."""
        snapshot = self.snapshot()
        return {
            "enabled": snapshot["enabled"],
            "mode": snapshot["mode"],
            "window_s": snapshot["window_s"],
            "providers": {
                provider: {
                    key: metrics[key] for key in (
                        "request_count", "success_count", "failure_count",
                        "throttle_signal_count", "signal_ratio", "current_gap_s",
                        "max_inflight", "in_flight", "backoff_remaining_s", "skipped_count",
                    )
                }
                for provider, metrics in snapshot["providers"].items()
                if metrics["request_count"] or metrics["in_flight"] or metrics["backoff_remaining_s"]
            },
        }


def _env_bool(name: str, default: bool) -> bool:
    value = os.environ.get(name)
    return default if value is None else value.strip().lower() in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning("Invalid %s; using default %.3f", name, default)
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        logger.warning("Invalid %s; using default %d", name, default)
        return default


_adaptive_provider_limiter: Optional[AdaptiveProviderLimiter] = None
_adaptive_provider_limiter_lock = threading.Lock()


def get_adaptive_provider_limiter() -> AdaptiveProviderLimiter:
    """Return the shared process-wide limiter used by workers and API routes."""
    global _adaptive_provider_limiter
    if _adaptive_provider_limiter is None:
        with _adaptive_provider_limiter_lock:
            if _adaptive_provider_limiter is None:
                _adaptive_provider_limiter = AdaptiveProviderLimiter()
    return _adaptive_provider_limiter
