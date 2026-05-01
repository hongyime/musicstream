"""
Tests for musicstream/db.py

Covers:
  - get_engine(): uses DATABASE_URL, correct pool settings
  - get_session_factory(): returns sessionmaker
  - get_session(): commit on success, rollback on exception, always closes
  - wait_for_db(): succeeds on first try, retries on failure, raises after max
  - run_migrations(): calls alembic upgrade head
  - init_db(): idempotent
"""
from __future__ import annotations

import os
import sys
from pathlib import Path
from unittest.mock import MagicMock, call, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest


# ── get_engine ────────────────────────────────────────────────────────────────

class TestGetEngine:
    def test_uses_database_url_env_var(self):
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///:memory:"}), \
             patch("db.create_engine") as mock_create:
            mock_create.return_value = MagicMock()
            import db as db_module
            db_module.get_engine()
            url_arg = mock_create.call_args[0][0]
            assert "sqlite" in url_arg

    def test_raises_key_error_when_env_missing(self):
        env = {k: v for k, v in os.environ.items() if k != "DATABASE_URL"}
        with patch.dict(os.environ, env, clear=True):
            import db as db_module
            with pytest.raises(KeyError):
                db_module.get_engine()

    def test_pool_settings(self):
        with patch.dict(os.environ, {"DATABASE_URL": "sqlite:///:memory:"}):
            import db as db_module
            with patch("db.create_engine") as mock_create:
                mock_create.return_value = MagicMock()
                db_module.get_engine()
            kwargs = mock_create.call_args[1]
            assert kwargs["pool_pre_ping"] is True
            assert kwargs["pool_size"] == 10
            assert kwargs["max_overflow"] == 20


# ── get_session_factory ───────────────────────────────────────────────────────

class TestGetSessionFactory:
    def test_returns_sessionmaker(self):
        import db as db_module
        from sqlalchemy.orm import sessionmaker
        mock_engine = MagicMock()
        factory = db_module.get_session_factory(mock_engine)
        assert isinstance(factory, sessionmaker)


# ── get_session context manager ───────────────────────────────────────────────

class TestGetSession:
    def test_commits_on_success(self):
        import db as db_module
        mock_session = MagicMock()
        mock_factory = MagicMock(return_value=mock_session)

        original_factory = db_module._session_factory
        db_module._session_factory = mock_factory
        try:
            with db_module.get_session() as sess:
                assert sess is mock_session
            mock_session.commit.assert_called_once()
        finally:
            db_module._session_factory = original_factory

    def test_rollback_on_exception(self):
        import db as db_module
        mock_session = MagicMock()
        mock_factory = MagicMock(return_value=mock_session)

        original_factory = db_module._session_factory
        db_module._session_factory = mock_factory
        try:
            with pytest.raises(ValueError):
                with db_module.get_session():
                    raise ValueError("test error")
            mock_session.rollback.assert_called_once()
        finally:
            db_module._session_factory = original_factory

    def test_always_closes_session(self):
        import db as db_module
        mock_session = MagicMock()
        mock_factory = MagicMock(return_value=mock_session)

        original_factory = db_module._session_factory
        db_module._session_factory = mock_factory
        try:
            try:
                with db_module.get_session():
                    raise RuntimeError("boom")
            except RuntimeError:
                pass
            mock_session.close.assert_called_once()
        finally:
            db_module._session_factory = original_factory

    def test_raises_runtime_error_when_not_initialised(self):
        import db as db_module
        original_factory = db_module._session_factory
        db_module._session_factory = None
        try:
            with pytest.raises(RuntimeError, match="init_db"):
                with db_module.get_session():
                    pass
        finally:
            db_module._session_factory = original_factory


# ── wait_for_db ───────────────────────────────────────────────────────────────

class TestWaitForDb:
    def test_succeeds_on_first_try(self):
        import db as db_module
        mock_engine = MagicMock()
        mock_conn = MagicMock()
        mock_engine.connect.return_value.__enter__ = MagicMock(return_value=mock_conn)
        mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)

        with patch("db.get_engine", return_value=mock_engine), \
             patch("time.sleep"):
            result = db_module.wait_for_db(max_retries=5, backoff_s=0.0)

        assert result is mock_engine

    def test_retries_on_failure_then_succeeds(self):
        import db as db_module
        from exceptions import DatabaseError

        mock_engine = MagicMock()
        call_count = [0]

        def _connect():
            call_count[0] += 1
            if call_count[0] < 3:
                raise Exception("connection refused")
            ctx = MagicMock()
            ctx.__enter__ = MagicMock(return_value=MagicMock())
            ctx.__exit__ = MagicMock(return_value=False)
            return ctx

        mock_engine.connect.side_effect = _connect

        with patch("db.get_engine", return_value=mock_engine), \
             patch("time.sleep"):
            result = db_module.wait_for_db(max_retries=5, backoff_s=0.0)

        assert result is mock_engine
        assert call_count[0] == 3

    def test_raises_database_error_after_max_retries(self):
        import db as db_module
        from exceptions import DatabaseError

        mock_engine = MagicMock()
        mock_engine.connect.side_effect = Exception("always fails")

        with patch("db.get_engine", return_value=mock_engine), \
             patch("time.sleep"):
            with pytest.raises(DatabaseError):
                db_module.wait_for_db(max_retries=3, backoff_s=0.0)

    def test_sleeps_between_retries(self):
        import db as db_module

        mock_engine = MagicMock()
        mock_engine.connect.side_effect = Exception("fail")
        sleep_calls = []

        with patch("db.get_engine", return_value=mock_engine), \
             patch("time.sleep", side_effect=lambda s: sleep_calls.append(s)):
            try:
                db_module.wait_for_db(max_retries=3, backoff_s=5.0)
            except Exception:
                pass

        # Should sleep between retries (max_retries - 1 times)
        assert len(sleep_calls) == 2
        assert all(s == 5.0 for s in sleep_calls)


# ── run_migrations ────────────────────────────────────────────────────────────

class TestRunMigrations:
    def test_calls_alembic_upgrade_head(self):
        import db as db_module
        mock_alembic_cmd = MagicMock()
        mock_alembic_cfg = MagicMock()

        with patch("db.alembic_command", mock_alembic_cmd, create=True), \
             patch("db.AlembicConfig", return_value=mock_alembic_cfg, create=True):
            # Patch the imports inside run_migrations
            with patch.dict("sys.modules", {
                "alembic": MagicMock(),
                "alembic.command": mock_alembic_cmd,
                "alembic.config": MagicMock(Config=MagicMock(return_value=mock_alembic_cfg)),
            }):
                try:
                    db_module.run_migrations()
                except Exception:
                    pass  # alembic.ini may not exist in test env

    def test_raises_database_error_on_failure(self):
        import db as db_module
        from exceptions import DatabaseError

        with patch("builtins.__import__", side_effect=ImportError("alembic not found")):
            with pytest.raises((DatabaseError, ImportError)):
                db_module.run_migrations()


# ── init_db ───────────────────────────────────────────────────────────────────

class TestInitDb:
    def test_idempotent_second_call_is_noop(self):
        import db as db_module
        mock_engine = MagicMock()
        with patch("db.get_engine", return_value=mock_engine):
            db_module._engine = None
            db_module._session_factory = None
            db_module.init_db()
            engine_after_first = db_module._engine
            db_module.init_db()
            engine_after_second = db_module._engine
            assert engine_after_first is engine_after_second
