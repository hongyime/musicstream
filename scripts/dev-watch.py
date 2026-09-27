#!/usr/bin/env python3
"""Run source-synchronized development without Docker access to the host checkout.

Requires Python 3.10+ and Docker Compose with watch/initial_sync support.
Build dependency images explicitly before running this command. Ctrl-C stops
the development stack; its named data volumes and reusable images are retained.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import shlex
import signal
import stat
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]
RULES = {
    "daemon": {
        "paths": [("src", "/app/src"), ("migrations", "/app/migrations"),
                  ("alembic.ini", "/app/alembic.ini")],
        "ready": ["/app/src/daemon.py", "/app/migrations/env.py", "/app/alembic.ini"],
        "command": ["uvicorn", "src.daemon:app", "--host", "0.0.0.0", "--port", "9079",
                    "--reload", "--reload-dir", "/app/src", "--reload-dir", "/app/migrations"],
    },
    "frontend": {
        "paths": [("frontend", "/app/frontend")],
        "ready": ["/app/frontend/src/App.tsx", "/app/frontend/index.html", "/app/frontend/vite.config.ts"],
        "command": ["npm", "run", "dev", "--", "--host", "0.0.0.0"],
    },
}
IGNORED_DIRS = {".git", ".venv", "venv", "node_modules", "__pycache__", "dist", "build",
                "vendor", "data", "logs", "backups", "credentials", "secrets"}
MANIFESTS = {"package.json", "package-lock.json", "pnpm-lock.yaml", "yarn.lock", "bun.lock",
             "bun.lockb", "pyproject.toml", "poetry.lock", "uv.lock", "go.mod", "go.sum"}
WATCH_IGNORE = ["**/.env*", "**/*secret*", "**/*credential*", "**/*.pem", "**/*.key",
                "**/*.p12", "**/*.pfx", "**/*.pyc", "**/requirements*.txt"]
WATCH_IGNORE += [f"**/{name}/**" for name in sorted(IGNORED_DIRS)]
WATCH_IGNORE += [f"**/{name}" for name in sorted(MANIFESTS)]
# Compose patterns are case-sensitive on Linux. Match the same protected names
# as the seed filter even when a file is newly created with mixed capitalization.
WATCH_IGNORE = ["".join(f"[{c.lower()}{c.upper()}]" if c.isascii() and c.isalpha() else c
                        for c in pattern) for pattern in WATCH_IGNORE]


def excluded(name: str) -> bool:
    lower = name.lower()
    return (lower in IGNORED_DIRS or lower in MANIFESTS or lower.startswith(".env")
            or "secret" in lower or "credential" in lower
            or lower.endswith((".pem", ".key", ".p12", ".pfx", ".pyc"))
            or (lower.startswith("requirements") and lower.endswith(".txt")))


def is_link(path: Path) -> bool:
    attributes = getattr(path.lstat(), "st_file_attributes", 0)
    return path.is_symlink() or bool(attributes & getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400))


def seed_source(source: Path, destination: Path, root: Path) -> int:
    """Copy only explicit code trees; never follow links or preserve stale mtimes."""
    if is_link(source):
        raise ValueError(f"Source links are not supported: {source.name}")
    if not source.resolve().is_relative_to(root.resolve()):
        raise ValueError("Source path escapes the checkout")
    if excluded(source.name):
        return 0
    if source.is_file():
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_bytes(source.read_bytes())
        return 1
    if not source.is_dir():
        raise FileNotFoundError(f"Missing development source: {source.name}")
    destination.mkdir(parents=True, exist_ok=True)
    count = 0
    for child in source.iterdir():
        if excluded(child.name) or is_link(child):
            continue
        count += seed_source(child, destination / child.name, root)
    return count


def synchronize_model(model: dict, seed: Path, root: Path) -> int:
    count = 0
    copied: set[str] = set()
    for name, rules in RULES.items():
        service = model["services"][name]
        targets = {target for _, target in rules["paths"]}
        volumes = []
        for volume in service.get("volumes", []):
            if volume["type"] == "bind":
                if volume["target"] not in targets:
                    raise ValueError(f"Unreviewed bind mount in {name}; use a local checkout or review its source rule")
                continue
            # Sync excludes dependencies, so the image's node_modules remains
            # visible without the anonymous shield required by a broad bind.
            if volume["type"] == "volume" and not volume.get("source") and volume["target"] == "/app/frontend/node_modules":
                continue
            volumes.append(volume)
        service["volumes"] = volumes
        service["pull_policy"] = "never"
        watch = []
        required_files = set(rules["ready"])
        for relative, target in rules["paths"]:
            if relative not in copied:
                count += seed_source(root / relative, seed / relative, root)
                copied.add(relative)
            seeded = seed / relative
            if seeded.is_file():
                required_files.add(target)
            else:
                for filename in seeded.rglob("*"):
                    if filename.is_file():
                        required_files.add(target + "/" + filename.relative_to(seeded).as_posix())
            for directory in (seed, root):
                watch.append({"action": "sync", "path": str(directory / relative),
                              "target": target, "initial_sync": True, "ignore": WATCH_IGNORE})
        service["develop"] = {"watch": watch}
        # Wait for every seeded file, not just the entry point: imports may be
        # copied later in the initial sync. Subsequent edits need no gate.
        ready = " && ".join("test -f " + shlex.quote(path) for path in sorted(required_files))
        command = " ".join(shlex.quote(part) for part in rules["command"])
        if len(ready.encode()) > 60000:
            raise ValueError("Source set is too large for this bounded startup gate")
        service["command"] = ["sh", "-c", f"until {ready}; do sleep 1; done; exec {command}"]
    if any(v["type"] == "bind" for s in model["services"].values() for v in s.get("volumes", [])):
        raise ValueError("This sync command requires every bind mount to have a reviewed source rule")
    return count


def stop_watcher(process: subprocess.Popen | None) -> None:
    if process is None or process.poll() is not None:
        return
    if os.name == "nt":
        try:
            process.send_signal(signal.CTRL_BREAK_EVENT)
            process.wait(timeout=10)
            return
        except (OSError, subprocess.TimeoutExpired):
            # Only the process tree launched below is eligible for termination.
            subprocess.run(["taskkill", "/PID", str(process.pid), "/T", "/F"],
                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)
    else:
        try:
            os.killpg(process.pid, signal.SIGTERM)
        except ProcessLookupError:
            return
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        if os.name != "nt":
            try:
                os.killpg(process.pid, signal.SIGKILL)
            except ProcessLookupError:
                return
        else:
            process.kill()
        process.wait(timeout=10)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default="musicstream-dev", help="isolated Compose project name")
    parser.add_argument("--settings", type=Path, help="explicit private settings passed to Compose")
    parser.add_argument("--discard-data", action="store_true", help="delete this dev project's volumes on exit")
    parser.add_argument("--check", action="store_true", help="validate/seed only; do not start Docker resources")
    args = parser.parse_args()
    if not args.project.endswith("-dev"):
        parser.error("The project name must end in -dev to keep lifecycle commands separate from production")
    watcher = None
    started = False
    result = 0
    with tempfile.TemporaryDirectory(prefix="musicstream-watch-") as directory:
        temporary = Path(directory)
        empty = temporary / "empty-settings"
        empty.write_text("", encoding="utf-8")
        # Suppress implicit .env discovery. Private settings are processed by
        # Compose only; this helper never reads or prints their contents.
        settings = args.settings.resolve() if args.settings else empty
        base = ["docker", "compose", "--env-file", str(settings), "--project-directory", str(ROOT),
                "--project-name", args.project, "-f", str(ROOT / "compose.dev.yaml")]
        rendered = subprocess.run([*base, "config", "--no-env-resolution", "--format", "json"],
                                  capture_output=True, text=True, check=False)
        if rendered.returncode:
            print("Compose configuration failed. Check the explicit dev settings and Compose version.")
            return rendered.returncode
        model = json.loads(rendered.stdout)
        files = synchronize_model(model, temporary / "source", ROOT)
        config = temporary / "compose.json"
        config.write_text(json.dumps(model), encoding="utf-8")
        compose = [*base[:-2], "-f", str(config)]
        subprocess.run([*compose, "config", "--quiet"], check=True)
        print(f"Prepared {files} source files; manifests and private files are excluded from synchronization.")
        if args.check:
            return 0
        existing = subprocess.run([*compose, "ps", "--all", "--quiet"],
                                  capture_output=True, text=True, check=True)
        if existing.stdout.strip():
            print(f"Project {args.project} already has containers. Stop that dev project before using this command.")
            return 1
        try:
            started = True
            subprocess.run([*compose, "up", "--detach", "--no-build", "--pull", "never"], check=True)
            process_options = ({"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP}
                               if os.name == "nt" else {"start_new_session": True})
            watcher = subprocess.Popen([*compose, "watch", "--no-up", "--prune=false", *RULES], **process_options)
            result = watcher.wait()
        except KeyboardInterrupt:
            print("Stopping the development stack.")
        finally:
            try:
                stop_watcher(watcher)
            finally:
                if started:
                    down = [*compose, "down", "--remove-orphans", "--timeout", "15"]
                    if args.discard_data:
                        down.append("--volumes")
                    teardown = subprocess.run(down, check=False)
                    if teardown.returncode:
                        print(f"Teardown failed; inspect only Compose project {args.project}.")
                        result = teardown.returncode
    return result


if __name__ == "__main__":
    raise SystemExit(main())
