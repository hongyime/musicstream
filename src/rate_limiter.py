"""
rate_limiter.py — Per-service rate limiting with exponential backoff, jitter,
and a circuit breaker for the musicstream daemon.

Services covered (PRD §11):
  spotify, librespot, youtube, ytmusicapi, spotdl,
  musicbrainz, acoustid, listenbrainz, coverart, soundcloud
"""

from __future__ import annotations

import json
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


# ── Throttle config (immutable, per service) ──────────────────────────────────

@dataclass(frozen=True)
class ThrottleConfig:
    """Floor/ceiling for AIMD inter-call spacing with randomised jitter."""
    floor: float          # minimum gap between calls (seconds)
    ceiling: float        # maximum gap after AIMD backoff
    jitter: float = 0.5   # upper-bound multiplier: actual gap ∈ [floor, floor × (1+jitter)]


# ── Throttle state (mutable, per service) ─────────────────────────────────────

@dataclass
class _ThrottleState:
    min_gap: float
    last_call: float = 0.0   # monotonic timestamp of last reserved slot


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


# ── AIMD per-service throttle ─────────────────────────────────────────────────

class ServiceThrottle:
    """
    Proactive inter-call spacing per service, shared across all worker threads.

    Enforces a minimum gap between consecutive calls to the same service using
    AIMD: 429/rate-limit → gap × 2 (up to ceiling); success → gap × 0.9 (down
    to floor).  Slot reservation is atomic — workers queue at exactly min_gap
    intervals rather than all firing simultaneously.

    wait() returns False when the computed wait exceeds SKIP_THRESHOLD,
    signalling the caller to skip the tier this pass and retry next run.
    """

    CONFIGS: Dict[str, ThrottleConfig] = {
        "youtube":    ThrottleConfig(floor=4.5, ceiling=60.0),   # random(4.5, 6.75)
        "soundcloud": ThrottleConfig(floor=1.5, ceiling=30.0),   # random(1.5, 2.25)
        "spotdl":     ThrottleConfig(floor=4.5, ceiling=60.0),   # random(4.5, 6.75)
        "spotiflac":   ThrottleConfig(floor=5.0, ceiling=60.0),   # random(5.0, 7.5)
    }

    SKIP_THRESHOLD: float = 30.0  # seconds; skip tier rather than block longer

    # Persistence: where to snapshot the current min_gap per service so an
    # AIMD backoff state survives daemon restarts. Without this, every
    # restart drops back to the floor, hammering remote services straight
    # into rate-limit responses again. (audit #13)
    _PERSIST_PATH: str = os.environ.get(
        "THROTTLE_STATE_PATH", "/app/data/throttle_state.json"
    )

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._state: Dict[str, _ThrottleState] = {
            svc: _ThrottleState(min_gap=cfg.floor)
            for svc, cfg in self.CONFIGS.items()
        }
        self._restore()

    def _restore(self) -> None:
        """Best-effort load of last-known min_gap values from disk."""
        try:
            if not os.path.exists(self._PERSIST_PATH):
                return
            with open(self._PERSIST_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            for svc, payload in data.items():
                if svc not in self._state:
                    continue
                gap = float(payload.get("min_gap", self.CONFIGS[svc].floor))
                # Clamp to configured floor/ceiling in case the persisted file
                # was written under a different config.
                gap = max(self.CONFIGS[svc].floor, min(self.CONFIGS[svc].ceiling, gap))
                self._state[svc].min_gap = gap
            logger.info(
                "Throttle: restored min_gap state from %s (%d services)",
                self._PERSIST_PATH, len(data),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Throttle: failed to restore state from %s: %s",
                           self._PERSIST_PATH, exc)

    def _persist(self) -> None:
        """Best-effort write of current min_gap values to disk.

        Uses atomic-rename so a SIGKILL mid-write can't corrupt the file.
        Caller MUST hold self._lock.
        """
        try:
            os.makedirs(os.path.dirname(self._PERSIST_PATH) or ".", exist_ok=True)
            payload = {
                svc: {"min_gap": state.min_gap}
                for svc, state in self._state.items()
            }
            tmp = f"{self._PERSIST_PATH}.tmp.{os.getpid()}"
            with open(tmp, "w", encoding="utf-8") as fh:
                json.dump(payload, fh, indent=2)
                fh.flush()
                os.fsync(fh.fileno())
            os.replace(tmp, self._PERSIST_PATH)
        except Exception as exc:  # noqa: BLE001
            logger.debug("Throttle: persist failed (non-fatal): %s", exc)

    def wait(self, service: str) -> bool:
        """
        Block until the inter-call gap for *service* is satisfied.

        Atomically reserves a call slot so concurrent workers queue at
        min_gap intervals.

        Returns:
            True  — slot reserved, caller should proceed.
            False — computed wait > SKIP_THRESHOLD; caller should skip this tier.
        """
        with self._lock:
            now = time.monotonic()
            state = self._state[service]
            cfg = self.CONFIGS[service]
            # Randomise within [min_gap, min_gap × (1 + jitter)], capped at ceiling.
            high = min(cfg.ceiling, state.min_gap * (1.0 + cfg.jitter))
            actual_gap = random.uniform(state.min_gap, high)
            elapsed = now - state.last_call
            wait_time = max(0.0, actual_gap - elapsed)

            if wait_time > self.SKIP_THRESHOLD:
                logger.debug(
                    "Throttle skip: service=%s wait=%.1fs exceeds %.1fs threshold",
                    service, wait_time, self.SKIP_THRESHOLD,
                )
                return False

            # Reserve with the randomised gap — next worker queues after this slot.
            state.last_call = max(now, state.last_call) + actual_gap

        if wait_time > 0:
            logger.debug("Throttle wait: service=%s %.1fs (gap=%.1f–%.1f)", service, wait_time, state.min_gap, high)
            time.sleep(wait_time)
        return True

    def on_success(self, service: str) -> None:
        """Decay min_gap 10% toward floor on each success (additive decrease)."""
        with self._lock:
            state = self._state[service]
            floor = self.CONFIGS[service].floor
            state.min_gap = max(floor, state.min_gap * 0.9)

    def on_rate_limit(self, service: str) -> None:
        """Double min_gap up to ceiling on rate-limit signal (multiplicative increase)."""
        with self._lock:
            state = self._state[service]
            ceiling = self.CONFIGS[service].ceiling
            old = state.min_gap
            state.min_gap = min(ceiling, state.min_gap * 2.0)
            logger.warning(
                "Throttle backoff: service=%s %.1fs → %.1fs",
                service, old, state.min_gap,
            )
            # Persist on backoff only — successes are common (would be a hot
            # path), backoffs are rare and important to survive restarts.
            self._persist()

    def status(self) -> Dict[str, Dict[str, float]]:
        """Current throttle gaps for all services (monitoring/debug)."""
        with self._lock:
            return {
                svc: {"min_gap": state.min_gap, "floor": self.CONFIGS[svc].floor}
                for svc, state in self._state.items()
            }


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

    def observe_call(self, provider: str, callback: Callable[..., Any], *args: Any, **kwargs: Any) -> Any:
        """Observe a non-enforced provider call and capture HTTP/exception outcome."""
        permit = self.acquire(provider, enforce=False)
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


# ── Expiring resolution cache ──────────────────────────────────────────────────

class ExpiringResolutionCache:
    """
    A dict-like cache where every entry has an individual TTL.

    Entries are lazily evicted on access; no background thread is required.

    Example::

        cache = ExpiringResolutionCache(default_ttl=300)
        cache.set("spotify:abc123", "/media/Artist/Album/01 - Track.flac")
        path = cache.get("spotify:abc123")   # returns value or None if expired
    """

    def __init__(self, default_ttl: float = 300.0) -> None:
        """
        Args:
            default_ttl: Default time-to-live in seconds for new entries.
        """
        self._default_ttl = default_ttl
        self._store: Dict[str, tuple[Any, float]] = {}  # key → (value, expires_at)
        self._lock = threading.Lock()

    # ── Dict-like interface ────────────────────────────────────────────────────

    def set(self, key: str, value: Any, ttl: Optional[float] = None) -> None:
        """Store *value* under *key* with an optional per-entry *ttl* (seconds)."""
        expires_at = time.monotonic() + (ttl if ttl is not None else self._default_ttl)
        with self._lock:
            self._store[key] = (value, expires_at)

    def get(self, key: str, default: Any = None) -> Any:
        """Return the cached value for *key*, or *default* if missing / expired."""
        with self._lock:
            entry = self._store.get(key)
            if entry is None:
                return default
            value, expires_at = entry
            if time.monotonic() > expires_at:
                del self._store[key]
                return default
            return value

    def __contains__(self, key: str) -> bool:
        return self.get(key) is not None

    def __setitem__(self, key: str, value: Any) -> None:
        self.set(key, value)

    def __getitem__(self, key: str) -> Any:
        result = self.get(key)
        if result is None:
            raise KeyError(key)
        return result

    def invalidate(self, key: str) -> None:
        """Remove a single entry regardless of TTL."""
        with self._lock:
            self._store.pop(key, None)

    def clear(self) -> None:
        """Remove all entries."""
        with self._lock:
            self._store.clear()

    def purge_expired(self) -> int:
        """Eagerly remove all expired entries. Returns the number removed."""
        now = time.monotonic()
        with self._lock:
            expired = [k for k, (_, exp) in self._store.items() if now > exp]
            for k in expired:
                del self._store[k]
        return len(expired)

    def __len__(self) -> int:
        """Return the number of non-expired entries."""
        now = time.monotonic()
        with self._lock:
            return sum(1 for _, (_, exp) in self._store.items() if now <= exp)

    def __repr__(self) -> str:
        return f"ExpiringResolutionCache(size={len(self)}, default_ttl={self._default_ttl}s)"


# ── Chaos monkey (testing utility) ────────────────────────────────────────────

class MusicDownloadChaosMonkey:
    """
    Testing utility that randomly injects failures into download operations.

    Disabled by default.  Enable only in test environments — never in
    production.

    Example::

        chaos = MusicDownloadChaosMonkey(enabled=True, failure_rate=0.2)
        chaos.inject_chaos("network", "download_track")  # 20% chance of raising
    """

    #: Preset failure rates for named intensity levels.
    INTENSITY_PRESETS: Dict[str, float] = {
        "low":    0.05,   # 5 %
        "medium": 0.20,   # 20 %
        "high":   0.50,   # 50 %
    }

    def __init__(
        self,
        enabled: bool = False,
        failure_rate: Optional[float] = None,
        intensity: str = "low",
    ) -> None:
        """
        Args:
            enabled:      Whether chaos injection is active.
            failure_rate: Explicit probability in ``[0, 1]``.  If ``None``,
                          the rate is derived from *intensity*.
            intensity:    Named preset (``"low"``, ``"medium"``, ``"high"``)
                          used when *failure_rate* is not given.
        """
        self.enabled = enabled
        if failure_rate is not None:
            if not 0.0 <= failure_rate <= 1.0:
                raise ValueError("failure_rate must be in [0, 1]")
            self.failure_rate = failure_rate
        else:
            if intensity not in self.INTENSITY_PRESETS:
                raise ValueError(
                    f"Unknown intensity '{intensity}'. "
                    f"Valid values: {sorted(self.INTENSITY_PRESETS)}"
                )
            self.failure_rate = self.INTENSITY_PRESETS[intensity]

        self._lock = threading.Lock()
        self._inject_count = 0
        self._call_count = 0

    # ── Public API ─────────────────────────────────────────────────────────────

    def inject_chaos(
        self,
        failure_type: str = "generic",
        operation: str = "unknown",
        exception_factory: Optional[Callable[[], Exception]] = None,
    ) -> None:
        """
        Randomly raise an exception based on the configured failure rate.

        Args:
            failure_type:       Label for the kind of failure (e.g. ``"network"``).
            operation:          Human-readable name of the operation being tested.
            exception_factory:  Callable that returns the exception to raise.
                                Defaults to ``RuntimeError``.

        Raises:
            Exception: The exception produced by *exception_factory* (or a
                       ``RuntimeError`` if none is provided) when chaos fires.
        """
        if not self.enabled:
            return

        with self._lock:
            self._call_count += 1
            should_fail = random.random() < self.failure_rate

        if should_fail:
            with self._lock:
                self._inject_count += 1
            exc = (
                exception_factory()
                if exception_factory is not None
                else RuntimeError(
                    f"[ChaosMonkey] Injected {failure_type} failure in '{operation}'"
                )
            )
            logger.debug(
                "ChaosMonkey fired: type=%s operation=%s rate=%.0f%%",
                failure_type, operation, self.failure_rate * 100,
            )
            raise exc

    @property
    def stats(self) -> Dict[str, Any]:
        """Return injection statistics (calls, injections, effective rate)."""
        with self._lock:
            calls = self._call_count
            injections = self._inject_count
        return {
            "enabled": self.enabled,
            "configured_rate": self.failure_rate,
            "calls": calls,
            "injections": injections,
            "effective_rate": injections / calls if calls else 0.0,
        }

    def reset_stats(self) -> None:
        """Reset call and injection counters."""
        with self._lock:
            self._call_count = 0
            self._inject_count = 0

    def __repr__(self) -> str:
        return (
            f"MusicDownloadChaosMonkey("
            f"enabled={self.enabled}, "
            f"failure_rate={self.failure_rate:.0%})"
        )
