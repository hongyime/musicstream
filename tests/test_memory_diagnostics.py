import asyncio
import json
import os
import signal
import tracemalloc
from concurrent.futures import ThreadPoolExecutor
from io import StringIO
from pathlib import Path
from threading import Event
from types import ModuleType, SimpleNamespace
from unittest.mock import Mock

import pytest


@pytest.fixture()
def diagnostic_daemon(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> ModuleType:
    import src.daemon as daemon

    output = tmp_path / "tracemalloc.jsonl"
    monkeypatch.setattr(daemon, "LOG_DIR", tmp_path)
    monkeypatch.setattr(daemon, "Path", lambda _: output)
    monkeypatch.setattr(
        daemon, "open", lambda _: StringIO("VmRSS: 2048 kB\n"), raising=False,
    )
    monkeypatch.setattr(tracemalloc, "is_tracing", lambda: True)
    monkeypatch.setattr(tracemalloc, "get_traced_memory", lambda: (3145728, 4194304))
    monkeypatch.setattr(tracemalloc, "get_tracemalloc_memory", lambda: 5242880)
    snapshot = tracemalloc.Snapshot([
        (0, 1048576, (("/app/src/worker.py", 17),), 1),
        (0, 8388608, ((tracemalloc.__file__, 558),), 1),
        (0, 1024, (("<frozen importlib._bootstrap>", 1),), 1),
    ], 1)
    monkeypatch.setattr(tracemalloc, "take_snapshot", lambda: snapshot)
    return daemon


def test_dump_reports_process_and_total_memory(
    diagnostic_daemon: ModuleType, tmp_path: Path,
) -> None:
    diagnostic_daemon._tracemalloc_dump()

    entry = json.loads((tmp_path / "tracemalloc.jsonl").read_text())
    assert entry["pid"] == os.getpid(), "Snapshots must identify the sampled process"
    assert entry["rss_mib"] == 2.0
    assert entry["traced_mib"] == 3.0, "Top-15 allocations are not the total heap"
    assert entry["traced_peak_mib"] == 4.0
    assert entry["tracer_mib"] == 5.0, "Profiler overhead must be visible separately"


def test_dump_filters_aggregated_stats_without_copying_all_traces(
    diagnostic_daemon: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(tracemalloc.Snapshot, "filter_traces", Mock(
        side_effect=AssertionError("Per-allocation filtering duplicates the snapshot")
    ))

    diagnostic_daemon._tracemalloc_dump()

    output = tmp_path / "tracemalloc.jsonl"
    assert output.exists(), "Dump must complete without copying all traces"
    entry = json.loads(output.read_text())
    assert entry["top"] == [
        {"size_mib": 1.0, "count": 1, "loc": "/app/src/worker.py:17"}
    ]


def test_overlapping_dump_is_coalesced(
    diagnostic_daemon: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    entered = Event()
    release = Event()
    snapshot = tracemalloc.take_snapshot()

    def slow_snapshot() -> tracemalloc.Snapshot:
        if not entered.is_set():
            entered.set()
            assert release.wait(5), "Test must release the in-progress snapshot"
        return snapshot

    take_snapshot = Mock(side_effect=slow_snapshot)
    monkeypatch.setattr(tracemalloc, "take_snapshot", take_snapshot)
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(diagnostic_daemon._tracemalloc_dump)
        try:
            assert entered.wait(5), "First dump must reach the snapshot boundary"
            diagnostic_daemon._tracemalloc_dump()
            assert take_snapshot.call_count == 1, "Concurrent dumps must not overlap"
        finally:
            release.set()
        first.result(timeout=5)


def test_untraced_dump_records_rss_without_allocating_snapshot(
    diagnostic_daemon: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    monkeypatch.setattr(tracemalloc, "is_tracing", lambda: False)
    take_snapshot = Mock()
    monkeypatch.setattr(tracemalloc, "take_snapshot", take_snapshot)

    diagnostic_daemon._tracemalloc_dump()

    take_snapshot.assert_not_called()
    output = tmp_path / "tracemalloc.jsonl"
    assert output.exists(), "RSS monitoring must work without the expensive profiler"
    entry = json.loads(output.read_text())
    assert entry["rss_mib"] == 2.0
    assert entry["tracing_enabled"] is False
    assert entry["traced_mib"] is None
    assert entry["top"] == []


def test_dump_uptime_describes_sample_before_processing(
    diagnostic_daemon: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path,
) -> None:
    clock = [10.0]
    snapshot = tracemalloc.take_snapshot()

    def finish_later() -> tracemalloc.Snapshot:
        clock[0] = 900.0
        return snapshot

    monkeypatch.setattr(diagnostic_daemon, "_start_time", 0.0)
    monkeypatch.setattr(diagnostic_daemon, "time", SimpleNamespace(
        time=lambda: clock[0], monotonic=diagnostic_daemon.time.monotonic,
    ))
    monkeypatch.setattr(tracemalloc, "take_snapshot", finish_later)

    diagnostic_daemon._tracemalloc_dump()

    entry = json.loads((tmp_path / "tracemalloc.jsonl").read_text())
    assert entry["uptime_s"] == 10.0, "Uptime must match sample time, not completion"


@pytest.mark.asyncio
async def test_rss_signal_registered_without_heap_tracing(
    diagnostic_daemon: ModuleType, monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("TRACEMALLOC_ENABLED", "0")
    monkeypatch.setenv("SKIP_BACKGROUND_STARTUP", "true")
    monkeypatch.setattr("src.db.wait_for_db", Mock())
    monkeypatch.setattr("src.db.init_db", Mock())
    monkeypatch.setattr("src.db.run_migrations", Mock())
    monkeypatch.setattr("src.ingestion.downloader.request_shutdown", Mock())
    monkeypatch.setattr(diagnostic_daemon.tasks, "reset_orphaned_downloads", Mock())
    monkeypatch.setattr(diagnostic_daemon, "scheduler", Mock())
    dumped = Event()
    monkeypatch.setattr(diagnostic_daemon, "_tracemalloc_dump", dumped.set)
    monkeypatch.setattr(signal, "SIGUSR1", signal.SIGTERM, raising=False)
    register = Mock()
    monkeypatch.setattr(asyncio.get_running_loop(), "add_signal_handler", register)

    async with diagnostic_daemon.lifespan(diagnostic_daemon.app):
        register.assert_called_once()
        callback = register.call_args.args[1]
        callback()
        assert await asyncio.to_thread(dumped.wait, 2), "Signal must dispatch RSS dump"
