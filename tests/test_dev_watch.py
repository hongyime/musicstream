"""Portable source-filter and lifecycle regression checks; no Docker resources."""
import copy
import fnmatch
import importlib.util
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest import mock

SPEC = importlib.util.spec_from_file_location("dev_watch", Path(__file__).resolve().parents[1] / "scripts/dev-watch.py")
WATCH = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(WATCH)


class WatchTests(unittest.TestCase):
    def fixture(self, root):
        for relative in ["src/daemon.py", "src/nested/module.py", "migrations/env.py", "alembic.ini",
                         "frontend/src/App.tsx", "frontend/index.html", "frontend/vite.config.ts"]:
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("synthetic fixture\n", encoding="utf-8")
        return {"name": "fixture-dev", "services": {
            "daemon": {"image": "daemon-dev:local", "build": {"context": str(root)},
                       "volumes": [{"type": "bind", "source": str(root / "src"), "target": "/app/src"},
                                   {"type": "volume", "source": "data", "target": "/app/data"}]},
            "frontend": {"image": "frontend-dev:local", "build": {"context": str(root)},
                         "volumes": [{"type": "bind", "source": str(root / "frontend"), "target": "/app/frontend"},
                                     {"type": "volume", "target": "/app/frontend/node_modules"}]},
            "postgres": {"image": "postgres:16-alpine"}}, "volumes": {"data": {}}}

    def test_seed_never_reads_protected_or_dependency_files_and_preserves_mtime(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            root.mkdir()
            self.fixture(root)
            excluded = ["frontend/.ENV.local", "frontend/MySecrets.json", "frontend/Private.KEY",
                        "frontend/package.json", "frontend/node_modules/dependency.js"]
            for name in excluded:
                path = root / name
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("inert exclusion fixture", encoding="utf-8")
            source = root / "frontend/src/App.tsx"
            before = source.stat().st_mtime_ns
            original = Path.read_bytes
            def guarded_read(path):
                self.assertNotIn(path.relative_to(root).as_posix(), excluded)
                return original(path)
            seed = Path(directory) / "seed"
            with mock.patch.object(Path, "read_bytes", guarded_read):
                WATCH.seed_source(root / "frontend", seed, root)
            self.assertEqual(source.stat().st_mtime_ns, before)
            self.assertEqual(sorted(p.relative_to(seed).as_posix() for p in seed.rglob("*") if p.is_file()),
                             ["index.html", "src/App.tsx", "vite.config.ts"])

    def test_live_ignore_patterns_cover_mixed_case_protected_names(self):
        for name in ["nested/.ENV.local", "nested/MySecrets.json", "nested/Private.KEY",
                     "nested/CREDENTIALS.txt", "nested/Package.JSON", "nested/Requirements-dev.TXT",
                     "nested/NODE_MODULES/module.js", "nested/BUILD/output.js"]:
            self.assertTrue(any(fnmatch.fnmatchcase(name, pattern) for pattern in WATCH.WATCH_IGNORE), name)

    def test_model_keeps_data_and_gates_every_source_file(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            root.mkdir()
            model = self.fixture(root)
            count = WATCH.synchronize_model(model, Path(directory) / "seed", root)
            self.assertEqual(count, 7)
            self.assertEqual(model["services"]["daemon"]["volumes"],
                             [{"type": "volume", "source": "data", "target": "/app/data"}])
            self.assertIn("test -f /app/src/nested/module.py", model["services"]["daemon"]["command"][2])
            self.assertTrue(all(rule["action"] == "sync" for name in WATCH.RULES
                                for rule in model["services"][name]["develop"]["watch"]))
            self.assertEqual(model["services"]["postgres"], {"image": "postgres:16-alpine"})
            self.assertEqual(model["services"]["frontend"]["volumes"], [])

    def test_unreviewed_host_data_mount_fails_closed(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            root.mkdir()
            model = self.fixture(root)
            model["services"]["daemon"]["volumes"].append({"type": "bind", "source": "/private", "target": "/private"})
            with self.assertRaisesRegex(ValueError, "Unreviewed bind mount"):
                WATCH.synchronize_model(model, Path(directory) / "seed", root)

    def exercise_lifecycle(self, *, existing=False, up_fails=False, stop_fails=False):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory) / "checkout"
            root.mkdir()
            model = self.fixture(root)
            calls = []
            generated_configs = []
            def run(args, **kwargs):
                calls.append(args)
                if "--format" in args:
                    return subprocess.CompletedProcess(args, 0, json.dumps(copy.deepcopy(model)), "")
                if "ps" in args:
                    return subprocess.CompletedProcess(args, 0, "existing-container" if existing else "", "")
                if "up" in args:
                    self.assertIn("--no-build", args)
                    self.assertEqual(args[args.index("--pull") + 1], "never")
                    generated_configs.append(Path(args[args.index("-f") + 1]))
                    if up_fails:
                        raise subprocess.CalledProcessError(7, args)
                return subprocess.CompletedProcess(args, 0, "", "")
            process = mock.Mock()
            process.wait.side_effect = KeyboardInterrupt
            stop_error = RuntimeError("fixture watcher cleanup failure") if stop_fails else None
            with mock.patch.object(WATCH, "ROOT", root), mock.patch("sys.argv", ["dev-watch.py", "--project", "fixture-dev"]), \
                 mock.patch.object(WATCH.subprocess, "run", run), mock.patch.object(WATCH.subprocess, "Popen", return_value=process), \
                 mock.patch.object(WATCH, "stop_watcher", side_effect=stop_error):
                if stop_fails:
                    with self.assertRaisesRegex(RuntimeError, "cleanup failure"):
                        WATCH.main()
                elif up_fails:
                    with self.assertRaises(subprocess.CalledProcessError):
                        WATCH.main()
                else:
                    self.assertEqual(WATCH.main(), 1 if existing else 0)
            down = [args for args in calls if "down" in args]
            self.assertEqual(len(down), 0 if existing else 1)
            if down:
                self.assertNotIn("--volumes", down[0])
                self.assertEqual(down[0][down[0].index("--project-name") + 1], "fixture-dev")
            self.assertFalse(any(path.exists() for path in generated_configs))
            self.assertFalse(any("build" in args or "pull" in args for args in calls))

    def test_ctrl_c_removes_only_owned_stack_and_temporary_source(self):
        self.exercise_lifecycle()

    def test_partial_start_failure_still_tears_down(self):
        self.exercise_lifecycle(up_fails=True)

    def test_watcher_cleanup_failure_still_tears_down(self):
        self.exercise_lifecycle(stop_fails=True)

    def test_existing_project_is_never_started_or_removed(self):
        self.exercise_lifecycle(existing=True)


if __name__ == "__main__":
    unittest.main()
