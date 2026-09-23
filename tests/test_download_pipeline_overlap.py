from concurrent.futures import ThreadPoolExecutor
from contextlib import nullcontext
from threading import Event
from unittest.mock import Mock

import pytest

from src.core import tasks


@pytest.fixture()
def pipeline_factory(monkeypatch: pytest.MonkeyPatch) -> Mock:
    orchestrator = Mock()
    orchestrator.download_pending_librespot.return_value = (0, 0)
    orchestrator.download_pending.return_value = (1, 0)
    orchestrator.download_pending_spotdl.return_value = (0, 0)
    factory = Mock(return_value=orchestrator)
    monkeypatch.setattr("src.ingestion.downloader.DownloadOrchestrator", factory)
    monkeypatch.setattr("src.db.get_session", nullcontext)
    monkeypatch.setattr(tasks, "reset_orphaned_downloads", Mock())
    monkeypatch.setattr(tasks, "_log_burn_rate", Mock())
    monkeypatch.setenv("LIBRESPOT_SWEEP_CONCURRENT", "false")
    return factory


@pytest.mark.parametrize("concurrent_sweep", ["true", "false"])
def test_overlapping_pipeline_does_not_create_more_workers(
    pipeline_factory: Mock, monkeypatch: pytest.MonkeyPatch, concurrent_sweep: str,
) -> None:
    entered = Event()
    release = Event()
    monkeypatch.setenv("LIBRESPOT_SWEEP_CONCURRENT", concurrent_sweep)

    def drain_pending(_session: None) -> tuple[int, int]:
        if not entered.is_set():
            entered.set()
            assert release.wait(5), "Test must release the active pipeline"
        return 1, 0

    pipeline_factory.return_value.download_pending.side_effect = drain_pending
    with ThreadPoolExecutor(max_workers=1) as pool:
        first = pool.submit(tasks.download_pipeline, 10)
        try:
            assert entered.wait(5), "First pipeline must reach the downloader"
            assert tasks.download_pipeline(11) == (0, 0), "Overlapping run must skip"
            pipeline_factory.assert_called_once_with(daemon_run_id=10)
        finally:
            release.set()
        assert first.result(timeout=5) == (1, 0)

    assert tasks.download_pipeline(12) == (1, 0), "Finished pipeline must release guard"
    assert pipeline_factory.call_count == 2


def test_pipeline_setup_failure_releases_guard(pipeline_factory: Mock) -> None:
    pipeline_factory.side_effect = [
        RuntimeError("setup failed"), pipeline_factory.return_value,
    ]
    assert tasks.download_pipeline(10) == (0, 0)
    assert tasks.download_pipeline(11) == (1, 0)
