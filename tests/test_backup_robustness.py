from __future__ import annotations

import os
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import src.core.tasks as tasks


def test_backup_publishes_only_complete_unique_dump(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(tasks, "_prune_backups", lambda: None)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@db/musicstream")

    def fake_run(command, **kwargs):
        dump_path = Path(command[command.index("--file") + 1])
        assert dump_path.suffix == ".tmp"
        dump_path.write_text("-- complete dump", encoding="utf-8")
        return MagicMock(returncode=0, stderr="")

    monkeypatch.setattr(tasks.subprocess, "run", fake_run)
    result = tasks.db_backup()

    assert result is not None
    published = Path(result)
    assert published.is_file()
    assert published.read_text(encoding="utf-8") == "-- complete dump"
    assert not list(tmp_path.glob("*.tmp"))
    assert not list(tmp_path.glob(".*.tmp"))


def test_backup_failure_removes_partial_dump_and_never_publishes(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "BACKUP_DIR", tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@db/musicstream")

    def fake_run(command, **kwargs):
        Path(command[command.index("--file") + 1]).write_text("partial", encoding="utf-8")
        return MagicMock(returncode=1, stderr="dump failed")

    monkeypatch.setattr(tasks.subprocess, "run", fake_run)
    assert tasks.db_backup() is None
    assert list(tmp_path.iterdir()) == []


def test_backup_lock_skips_overlapping_invocation(monkeypatch):
    monkeypatch.setattr(tasks.subprocess, "run", lambda *args, **kwargs: pytest.fail("pg_dump must not run"))
    tasks._BACKUP_LOCK.acquire()
    try:
        assert tasks.db_backup() is None
    finally:
        tasks._BACKUP_LOCK.release()


def test_backup_retention_uses_age_not_file_count(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "BACKUP_DIR", tmp_path)
    monkeypatch.setattr(tasks, "BACKUP_RETENTION_DAYS", 14)
    now = tasks.time.time()
    old = tmp_path / "musicstream_old.sql"
    recent = tmp_path / "musicstream_recent.sql"
    old.write_text("old", encoding="utf-8")
    recent.write_text("recent", encoding="utf-8")
    os.utime(old, (now - 15 * 86400, now - 15 * 86400))
    os.utime(recent, (now - 13 * 86400, now - 13 * 86400))

    tasks._prune_backups()

    assert not old.exists()
    assert recent.exists()


def test_restore_verification_stops_on_restore_error_and_cleans_database(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "BACKUP_DIR", tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@db/musicstream")
    dump = tmp_path / "musicstream_test.sql"
    dump.write_text("-- dump", encoding="utf-8")
    calls: list[list[str]] = []

    def fake_run(command, **kwargs):
        calls.append(command)
        if "-c" in command and command[command.index("-c") + 1].startswith("CREATE DATABASE"):
            return MagicMock(returncode=0, stderr="", stdout="")
        if kwargs.get("stdin") is not None:
            assert "ON_ERROR_STOP=1" in command
            return MagicMock(returncode=1, stderr="SQL restore error", stdout="")
        return MagicMock(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(tasks.subprocess, "run", fake_run)
    assert tasks.verify_backup_restore() is False
    assert any("DROP DATABASE IF EXISTS" in command for args in calls for command in args)


def test_restore_verification_checks_all_tables_and_track_data(tmp_path, monkeypatch):
    monkeypatch.setattr(tasks, "BACKUP_DIR", tmp_path)
    monkeypatch.setenv("DATABASE_URL", "postgresql://user:pw@db/musicstream")
    (tmp_path / "musicstream_test.sql").write_text("-- dump", encoding="utf-8")

    def fake_run(command, **kwargs):
        if "-c" in command and command[command.index("-c") + 1].startswith("CREATE DATABASE"):
            return MagicMock(returncode=0, stderr="", stdout="")
        if kwargs.get("stdin") is not None:
            return MagicMock(returncode=0, stderr="", stdout="")
        query = command[-1]
        if "information_schema.tables" in query:
            return MagicMock(
                returncode=0,
                stderr="",
                stdout=",".join(tasks._EXPECTED_RESTORE_TABLES),
            )
        if query == "SELECT count(*) FROM tracks;":
            return MagicMock(returncode=0, stderr="", stdout="12\n")
        return MagicMock(returncode=0, stderr="", stdout="")

    monkeypatch.setattr(tasks.subprocess, "run", fake_run)
    assert tasks.verify_backup_restore() is True


def test_monthly_restore_verification_is_scheduled():
    import src.daemon as daemon

    scheduler = MagicMock()
    original = daemon.scheduler
    daemon.scheduler = scheduler
    try:
        daemon._register_scheduler_jobs()
    finally:
        daemon.scheduler = original

    call = next(call for call in scheduler.add_job.call_args_list if call.kwargs.get("id") == "backup_restore_verify")
    assert call.args[0] is tasks.verify_backup_restore
    assert call.args[1] == "cron"
    assert call.kwargs["day"] == 1
    assert call.kwargs["hour"] == 6


def test_orphan_inventory_job_uses_configured_week_interval(monkeypatch):
    import src.daemon as daemon

    scheduler = MagicMock()
    monkeypatch.setattr(daemon, "scheduler", scheduler)
    monkeypatch.setattr(daemon, "ORPHAN_INVENTORY_INTERVAL_DAYS", 3)

    daemon._register_scheduler_jobs()

    call = next(
        call for call in scheduler.add_job.call_args_list
        if call.kwargs.get("id") == "orphan_file_inventory"
    )
    assert call.args[0] is tasks.log_orphan_file_inventory
    assert call.args[1] == "interval"
    assert call.kwargs["days"] == 3
