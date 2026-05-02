"""
migrations/env.py — Alembic environment configuration for musicstream.

Reads DATABASE_URL from os.environ and supports both online (live DB
connection) and offline (SQL script generation) migration modes.
"""

from __future__ import annotations

import os
from logging.config import fileConfig

from alembic import context
from sqlalchemy import engine_from_config, pool

# Import Base from models so Alembic can detect schema changes via autogenerate
from src.models import Base

# ── Alembic Config object ─────────────────────────────────────────────────────

config = context.config

# Override sqlalchemy.url with the DATABASE_URL environment variable.
# This must be set before any engine is created.
config.set_main_option("sqlalchemy.url", os.environ["DATABASE_URL"])

# Interpret the config file for Python logging if present.
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# Metadata for autogenerate support
target_metadata = Base.metadata


# ── Offline mode ──────────────────────────────────────────────────────────────

def run_migrations_offline() -> None:
    """
    Run migrations in 'offline' mode.

    Configures the context with just a URL and not an Engine.
    Calls to context.execute() emit the given string to the script output.
    """
    url = config.get_main_option("sqlalchemy.url")
    context.configure(
        url=url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
    )

    with context.begin_transaction():
        context.run_migrations()


# ── Online mode ───────────────────────────────────────────────────────────────

def run_migrations_online() -> None:
    """
    Run migrations in 'online' mode.

    Creates an Engine and associates a connection with the context.
    """
    connectable = engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    with connectable.connect() as connection:
        context.configure(
            connection=connection,
            target_metadata=target_metadata,
        )

        with context.begin_transaction():
            context.run_migrations()


# ── Entry point ───────────────────────────────────────────────────────────────

if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
