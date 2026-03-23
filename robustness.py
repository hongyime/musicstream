from __future__ import annotations

import random
import threading
import time
from collections import defaultdict, deque
from dataclasses import dataclass
from typing import Callable, Deque, Dict, Optional

from exceptions import SpotifyRateLimitError, YouTubeMusicRateLimitError


@dataclass(frozen=True)
class ServiceRateConfig:
    base_backoff: float
    max_backoff: float
    jitter_min: float
    jitter_max: float
    concurrent_limit: int


class DualServiceRateLimiter:
    def __init__(self) -> None:
        self._configs: Dict[str, ServiceRateConfig] = {
            "spotify": ServiceRateConfig(3.0, 3600.0, 0.0, 2.0, 10),
            "youtube_music": ServiceRateConfig(2.5, 300.0, 0.0, 1.5, 5),
            "youtube_direct": ServiceRateConfig(4.0, 600.0, 0.0, 3.0, 3),
            "network": ServiceRateConfig(2.0, 120.0, 0.0, 1.0, 20),
        }
        self._failure_counts: Dict[str, int] = defaultdict(int)
        self._service_health: Dict[str, Dict[str, float | int | bool]] = defaultdict(
            lambda: {"healthy": True, "last_check": time.time(), "failures": 0}
        )
        self._active_operations: Dict[str, Deque[float]] = defaultdict(deque)
        self._lock = threading.Lock()

    def begin_operation(self, service: str) -> None:
        with self._lock:
            self._cleanup_stale_operations(service)
            self._active_operations[service].append(time.time())

    def end_operation(self, service: str) -> None:
        with self._lock:
            if self._active_operations[service]:
                self._active_operations[service].popleft()

    def calculate_wait_time(
        self,
        service: str,
        attempt: int,
        retry_after: Optional[float] = None,
    ) -> float:
        config = self._configs.get(service, self._configs["network"])
        jitter = random.uniform(config.jitter_min, config.jitter_max)
        if retry_after is not None and retry_after > 0:
            return retry_after + jitter

        effective_attempt = max(1, attempt)
        base_wait = min(config.base_backoff ** effective_attempt, config.max_backoff)
        coordination_delay = self._coordination_delay(service)
        return base_wait + jitter + coordination_delay

    def register_failure(self, service: str) -> None:
        with self._lock:
            self._failure_counts[service] += 1
            health = self._service_health[service]
            health["healthy"] = False
            health["last_check"] = time.time()
            health["failures"] = int(health["failures"]) + 1

    def register_success(self, service: str) -> None:
        with self._lock:
            self._failure_counts[service] = 0
            health = self._service_health[service]
            health["healthy"] = True
            health["last_check"] = time.time()

    def failure_count(self, service: str) -> int:
        with self._lock:
            return int(self._failure_counts.get(service, 0))

    def health_snapshot(self) -> Dict[str, Dict[str, float | int | bool]]:
        with self._lock:
            return {
                key: {
                    "healthy": bool(value["healthy"]),
                    "last_check": float(value["last_check"]),
                    "failures": int(value["failures"]),
                }
                for key, value in self._service_health.items()
            }

    def _coordination_delay(self, service: str) -> float:
        with self._lock:
            self._cleanup_stale_operations(service)
            active_current = len(self._active_operations[service])
            active_other = sum(
                len(queue)
                for svc, queue in self._active_operations.items()
                if svc != service
            )
            config = self._configs.get(service, self._configs["network"])
            saturation = max(0, active_current - config.concurrent_limit)
            cross_pressure = min(active_other * 0.2, 4.0)
            return float(saturation) + cross_pressure

    def _cleanup_stale_operations(self, service: str) -> None:
        cutoff = time.time() - 60
        queue = self._active_operations[service]
        while queue and queue[0] < cutoff:
            queue.popleft()


class ExpiringResolutionCache:
    def __init__(self, max_entries: int = 4000, ttl_seconds: int = 1800) -> None:
        self._max_entries = max_entries
        self._ttl_seconds = ttl_seconds
        self._store: Dict[str, tuple[float, Optional[str]]] = {}
        self._keys: Deque[str] = deque()
        self._lock = threading.Lock()

    def get(self, key: str) -> Optional[str] | None:
        with self._lock:
            value = self._store.get(key)
            if value is None:
                return None
            created_at, video_id = value
            if time.time() - created_at > self._ttl_seconds:
                self._store.pop(key, None)
                return None
            return video_id

    def set(self, key: str, value: Optional[str]) -> None:
        with self._lock:
            self._store[key] = (time.time(), value)
            self._keys.append(key)
            while len(self._store) > self._max_entries and self._keys:
                oldest = self._keys.popleft()
                self._store.pop(oldest, None)


class MusicDownloadChaosMonkey:
    def __init__(self, enabled: bool = False, intensity: str = "low") -> None:
        self.enabled = enabled
        self.intensity = intensity
        base = {
            "spotify": {
                "token_expiration": 0.01,
                "rate_limit": 0.02,
                "service_unavailable": 0.01,
            },
            "youtube": {
                "region_block": 0.02,
                "content_id_block": 0.015,
                "rate_limit": 0.03,
            },
            "network": {
                "dns_failure": 0.01,
                "connection_drop": 0.015,
                "bandwidth_throttle": 0.01,
            },
            "system": {
                "disk_full": 0.005,
                "memory_pressure": 0.005,
                "permission_denied": 0.003,
            },
        }
        multiplier = {"low": 1.0, "medium": 2.0, "high": 3.5}.get(intensity, 1.0)
        self.failure_scenarios: Dict[str, Dict[str, float]] = {
            service: {
                failure_type: min(probability * multiplier, 0.6)
                for failure_type, probability in failures.items()
            }
            for service, failures in base.items()
        }

    def inject_chaos(self, service: str, operation: str) -> None:
        if not self.enabled:
            return
        scenarios = self.failure_scenarios.get(service)
        if not scenarios:
            return
        for failure_type, probability in scenarios.items():
            if random.random() < probability:
                self._raise_failure(service, failure_type, operation)

    def run_chaos_test_suite(
        self,
        duration_seconds: int,
        operation_runner: Callable[[str], None],
    ) -> Dict[str, object]:
        start = time.time()
        results: Dict[str, object] = {
            "total_operations": 0,
            "failed_operations": 0,
            "recovered_operations": 0,
            "failure_types": defaultdict(int),
            "recovery_times": [],
        }
        operations = [
            "spotify_playlist_fetch",
            "youtube_search",
            "download_track",
            "database_update",
        ]
        while time.time() - start < duration_seconds:
            operation = random.choice(operations)
            service = operation.split("_")[0]
            results["total_operations"] = int(results["total_operations"]) + 1
            operation_start = time.time()
            try:
                self.inject_chaos(service, operation)
                operation_runner(operation)
                elapsed = time.time() - operation_start
                cast_times = results["recovery_times"]
                if isinstance(cast_times, list):
                    cast_times.append(elapsed)
            except Exception as exc:
                results["failed_operations"] = int(results["failed_operations"]) + 1
                failure_types = results["failure_types"]
                if isinstance(failure_types, defaultdict):
                    failure_types[type(exc).__name__] += 1
                if self._is_recoverable(exc):
                    results["recovered_operations"] = int(results["recovered_operations"]) + 1
            time.sleep(0.02)
        return results

    def _is_recoverable(self, exc: Exception) -> bool:
        return isinstance(
            exc,
            (
                TimeoutError,
                ConnectionError,
                SpotifyRateLimitError,
                YouTubeMusicRateLimitError,
                OSError,
            ),
        )

    def _raise_failure(self, service: str, failure_type: str, _operation: str) -> None:
        key = f"{service}_{failure_type}"
        if key == "spotify_token_expiration":
            raise PermissionError("Spotify token expired")
        if key == "spotify_rate_limit":
            raise SpotifyRateLimitError("Injected chaos rate limit")
        if key == "spotify_service_unavailable":
            raise ConnectionError("Spotify service unavailable")
        if key == "youtube_region_block":
            raise PermissionError("Video unavailable in your region")
        if key == "youtube_content_id_block":
            raise PermissionError("Content blocked by rights holder")
        if key == "youtube_rate_limit":
            raise YouTubeMusicRateLimitError("Injected chaos rate limit")
        if key == "network_dns_failure":
            raise ConnectionError("DNS resolution failed")
        if key == "network_connection_drop":
            raise ConnectionError("Connection dropped")
        if key == "network_bandwidth_throttle":
            raise TimeoutError("Severe network throttling")
        if key == "system_disk_full":
            raise OSError("No space left on device")
        if key == "system_memory_pressure":
            raise MemoryError("System memory pressure")
        if key == "system_permission_denied":
            raise PermissionError("Permission denied")
