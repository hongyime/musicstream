"""Exercise the real lifespan cleanup without importing the application's services."""
import ast
import asyncio
import types
import unittest
from pathlib import Path
from unittest import mock


class FakeScheduler:
    def __init__(self, running):
        self.running = running
        self.shutdown_calls = 0

    def shutdown(self):
        self.shutdown_calls += 1
        if not self.running:
            raise RuntimeError("Scheduler is not running")
        self.running = False


class DevLifespanShutdownTests(unittest.TestCase):
    def cleanup_function(self, scheduler, drain_error=False):
        source = Path(__file__).resolve().parents[1] / "src" / "daemon.py"
        tree = ast.parse(source.read_text(encoding="utf-8"))
        lifespan = next(node for node in tree.body if isinstance(node, ast.AsyncFunctionDef) and node.name == "lifespan")
        yield_index = next(index for index, node in enumerate(lifespan.body) if isinstance(node, ast.Expr) and isinstance(node.value, ast.Yield))
        cleanup = ast.AsyncFunctionDef(
            name="cleanup", args=ast.arguments(posonlyargs=[], args=[], kwonlyargs=[], kw_defaults=[], defaults=[]),
            body=lifespan.body[yield_index + 1:], decorator_list=[],
        )
        drain = mock.Mock(return_value=0, side_effect=RuntimeError("fixture drain failed") if drain_error else None)
        dump = mock.Mock()
        logger = mock.Mock()
        namespace = {"scheduler": scheduler, "tasks": types.SimpleNamespace(reset_orphaned_downloads=drain), "_tracemalloc_dump": dump, "logger": logger}
        exec(compile(ast.fix_missing_locations(ast.Module(body=[cleanup], type_ignores=[])), str(source), "exec"), namespace)
        return namespace["cleanup"], drain, dump, logger

    def exercise_cleanup(self, running, drain_error=False):
        scheduler = FakeScheduler(running)
        cleanup, drain, dump, logger = self.cleanup_function(scheduler, drain_error)
        downloader = types.ModuleType("src.ingestion.downloader")
        downloader.request_shutdown = mock.Mock()
        with mock.patch.dict("sys.modules", {"src.ingestion.downloader": downloader}):
            asyncio.run(cleanup())
        downloader.request_shutdown.assert_called_once_with()
        drain.assert_called_once_with(all_rows=True)
        dump.assert_called_once_with()
        return scheduler, logger

    def test_skipped_background_scheduler_still_drains_without_shutdown_error(self):
        scheduler, _ = self.exercise_cleanup(running=False)
        self.assertEqual(scheduler.shutdown_calls, 0)

    def test_running_scheduler_is_stopped_once(self):
        scheduler, _ = self.exercise_cleanup(running=True)
        self.assertEqual(scheduler.shutdown_calls, 1)
        self.assertFalse(scheduler.running)

    def test_drain_failure_still_stops_a_running_scheduler(self):
        scheduler, logger = self.exercise_cleanup(running=True, drain_error=True)
        self.assertEqual(scheduler.shutdown_calls, 1)
        logger.warning.assert_called_once()


if __name__ == "__main__":
    unittest.main()
