from __future__ import annotations

import os
import subprocess
from pathlib import Path


_ENTRYPOINT = Path(__file__).resolve().parent.parent / "docker-entrypoint.sh"


def _run_entrypoint(tmp_path: Path, *, marker: bool, postgres_ok: bool = True, provider_ok: bool = True):
    media_dir = tmp_path / "media"
    media_dir.mkdir()
    if marker:
        (media_dir / ".musicstream-ready").touch()

    fake_bin = tmp_path / "bin"
    fake_bin.mkdir()
    (fake_bin / "gosu").write_text("#!/bin/sh\nshift\nexec \"$@\"\n", encoding="utf-8")
    (fake_bin / "pg_isready").write_text(f"#!/bin/sh\nexit {0 if postgres_ok else 1}\n", encoding="utf-8")
    (fake_bin / "curl").write_text(f"#!/bin/sh\nexit {0 if provider_ok else 1}\n", encoding="utf-8")
    for command in fake_bin.iterdir():
        command.chmod(0o755)

    env = os.environ | {
        "PATH": f"{fake_bin}:{os.environ['PATH']}",
        "MEDIA_DIR": str(media_dir),
        "RUNTIME_USER": "musicstream",
        "DEPENDENCY_WAIT_SECONDS": "0",
    }
    return subprocess.run(
        ["bash", str(_ENTRYPOINT), "sh", "-c", "exit 42"],
        env=env,
        text=True,
        capture_output=True,
        check=False,
    )


def test_entrypoint_refuses_missing_media_marker(tmp_path):
    result = _run_entrypoint(tmp_path, marker=False)

    assert result.returncode == 1
    assert "Media volume is not ready" in result.stderr


def test_entrypoint_refuses_unavailable_dependency(tmp_path):
    result = _run_entrypoint(tmp_path, marker=True, postgres_ok=False)

    assert result.returncode == 1
    assert "Timed out waiting for PostgreSQL" in result.stderr


def test_entrypoint_starts_only_after_marker_and_dependencies_are_ready(tmp_path):
    result = _run_entrypoint(tmp_path, marker=True)

    assert result.returncode == 42
