import os
import subprocess
import sys
from pathlib import Path


def test_migrations_preserve_daemon_diagnostic_logging() -> None:
    script = """
import logging
import runpy
from contextlib import nullcontext
from alembic import context
from alembic.config import Config

context.config = Config('alembic.ini')
context.is_offline_mode = lambda: True
context.configure = lambda **kwargs: None
context.begin_transaction = nullcontext
context.run_migrations = lambda: None
logger = logging.getLogger('musicstream.daemon')
logger.disabled = False
runpy.run_path('migrations/env.py')
assert not logger.disabled, 'Migrations must not disable daemon diagnostic logging'
"""
    result = subprocess.run(
        [sys.executable, "-c", script],
        cwd=Path(__file__).resolve().parents[1],
        env={**os.environ, "DATABASE_URL": "sqlite:///:memory:"},
        capture_output=True,
        text=True,
        timeout=60,
        check=False,
    )
    assert result.returncode == 0, result.stderr
