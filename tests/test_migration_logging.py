import logging
import runpy
import sys
from contextlib import nullcontext
from pathlib import Path
from unittest.mock import Mock
from weakref import WeakValueDictionary

import pytest
from alembic import context
from alembic.config import Config


def test_migrations_preserve_daemon_diagnostic_logging(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root_path = Path(__file__).resolve().parents[1]
    root_logger = logging.RootLogger(logging.WARNING)
    manager = logging.Manager(root_logger)
    monkeypatch.setattr(logging, "root", root_logger)
    monkeypatch.setattr(logging.Logger, "root", root_logger)
    monkeypatch.setattr(logging.Logger, "manager", manager)
    # Sandbox fileConfig's registry-wide handler shutdown, including pytest handlers.
    monkeypatch.setattr(logging, "_handlers", WeakValueDictionary())
    monkeypatch.setattr(logging, "_handlerList", [])
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setenv("DATABASE_URL", "sqlite:///:memory:")
    monkeypatch.setattr(
        context, "config", Config(str(root_path / "alembic.ini")), raising=False,
    )
    monkeypatch.setattr(context, "is_offline_mode", lambda: True)
    monkeypatch.setattr(context, "configure", Mock())
    monkeypatch.setattr(context, "begin_transaction", nullcontext)
    monkeypatch.setattr(context, "run_migrations", Mock())
    logger = logging.getLogger("musicstream.daemon")
    try:
        runpy.run_path(str(root_path / "migrations" / "env.py"))
        assert not logger.disabled, "Migrations must preserve daemon diagnostic logging"
    finally:
        for handler in root_logger.handlers:
            handler.close()
