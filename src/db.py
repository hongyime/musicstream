"""
musicstream/db.py — PostgreSQL engine and session factory

Provides:
  - get_engine()           : create SQLAlchemy Engine from DATABASE_URL
  - get_session_factory()  : create a sessionmaker bound to an engine
  - get_session()          : context manager yielding a Session
  - init_db()              : lazily initialise module-level engine + session factory
  - run_migrations()       : programmatically run `alembic upgrade head`
  - wait_for_db()          : retry loop until the DB is reachable
"""

from __future__ import annotations

import logging
import os
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Generator

from sqlalchemy import create_engine, text
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker

from src.exceptions import DatabaseError

logger = logging.getLogger(__name__)

# ── Module-level singletons (lazily initialised by init_db()) ─────────────────

_engine: Engine | None = None
_session_factory: sessionmaker | None = None


# ── Engine ────────────────────────────────────────────────────────────────────

def get_engine() -> Engine:
    """
    Create a SQLAlchemy Engine from the DATABASE_URL environment variable.

    Pool settings:
      pool_pre_ping=True  — verify connections before use
      pool_size=10        — keep up to 10 persistent connections
      max_overflow=20     — allow up to 20 extra connections under load
    """
    url = os.environ["DATABASE_URL"]
    return create_engine(
        url,
        pool_pre_ping=True,
        pool_size=10,
        max_overflow=20,
    )


# ── Session factory ───────────────────────────────────────────────────────────

def get_session_factory(engine: Engine) -> sessionmaker:
    """
    Return a sessionmaker bound to *engine*.

    expire_on_commit=False keeps ORM objects usable after a commit without
    triggering lazy-load queries.
    """
    return sessionmaker(bind=engine, expire_on_commit=False)


# ── Lazy initialisation ───────────────────────────────────────────────────────

def init_db() -> None:
    """
    Lazily initialise the module-level engine and session factory.

    Safe to call multiple times — subsequent calls are no-ops.
    """
    global _engine, _session_factory
    if _engine is None:
        _engine = get_engine()
        logger.debug("SQLAlchemy engine created.")
    if _session_factory is None:
        _session_factory = get_session_factory(_engine)
        logger.debug("Session factory created.")


# ── Session context manager ───────────────────────────────────────────────────

@contextmanager
def get_session() -> Generator[Session, None, None]:
    """
    Yield a database Session from the module-level session factory.

    On success  : commit.
    On exception: rollback and re-raise.
    Always      : close the session.

    Raises:
        RuntimeError: if init_db() has not been called yet.
    """
    if _session_factory is None:
        raise RuntimeError(
            "Database not initialised. Call init_db() before using get_session()."
        )

    session: Session = _session_factory()
    try:
        yield session
        session.commit()
    except Exception:
        session.rollback()
        raise
    finally:
        session.close()


# ── Alembic migrations ────────────────────────────────────────────────────────

def run_migrations() -> None:
    """
    Programmatically run ``alembic upgrade head``.

    Reads ``alembic.ini`` from the same directory as this file (``db.py``).

    Raises:
        DatabaseError: if the migration fails.
    """
    try:
        from alembic import command as alembic_command
        from alembic.config import Config as AlembicConfig

        ini_path = Path(__file__).parent.parent / "alembic.ini"
        alembic_cfg = AlembicConfig(str(ini_path))

        logger.info("Running Alembic migrations (upgrade head)…")
        alembic_command.upgrade(alembic_cfg, "head")
        logger.info("Alembic migrations completed successfully.")
    except Exception as exc:
        logger.error("Alembic migration failed: %s", exc)
        raise DatabaseError(f"Migration failed: {exc}") from exc


# ── DB readiness probe ────────────────────────────────────────────────────────

def wait_for_db(max_retries: int = 5, backoff_s: float = 5.0) -> Engine:
    """
    Attempt to connect to the database up to *max_retries* times.

    Sleeps *backoff_s* seconds between attempts.

    Returns:
        The connected Engine on success.

    Raises:
        DatabaseError: if all attempts fail.
    """
    engine = get_engine()

    for attempt in range(1, max_retries + 1):
        try:
            with engine.connect() as conn:
                conn.execute(text("SELECT 1"))
            logger.info("Database connection established (attempt %d/%d).", attempt, max_retries)
            return engine
        except Exception as exc:
            logger.warning(
                "Database not ready (attempt %d/%d): %s",
                attempt,
                max_retries,
                exc,
            )
            if attempt < max_retries:
                logger.info("Retrying in %.1f seconds…", backoff_s)
                time.sleep(backoff_s)

    msg = f"Could not connect to the database after {max_retries} attempts."
    logger.error(msg)
    raise DatabaseError(msg)
