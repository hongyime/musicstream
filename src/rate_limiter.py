"""
rate_limiter.py — Per-service rate limiting with exponential backoff, jitter,
and a circuit breaker for the musicstream daemon.

Services covered (PRD §11):
  spotify, spotiflac, youtube, ytmusicapi, spotdl,
  musicbrainz, acoustid, listenbrainz, coverart
"""

from __future__ import annotations

import logging
import random
import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, Optional

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
        "spotiflac":    ServiceRateConfig(base=5.0,  max=300,  concurrent=2),
        "youtube":      ServiceRateConfig(base=4.0,  max=600,  concurrent=3),
        "ytmusicapi":   ServiceRateConfig(base=2.5,  max=300,  concurrent=5),
        "spotdl":       ServiceRateConfig(base=3.0,  max=180,  concurrent=3),
        "musicbrainz":  ServiceRateConfig(base=1.0,  max=60,   concurrent=1),
        "acoustid":     ServiceRateConfig(base=0.5,  max=30,   concurrent=3),
        "listenbrainz": ServiceRateConfig(base=1.0,  max=60,   concurrent=5),
        "coverart":     ServiceRateConfig(base=0.5,  max=30,   concurrent=5),
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
