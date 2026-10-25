"""Alembic environment — async, driven by the app's own settings.

Purpose:    tells Alembic (the schema-migration tool) how to connect to the
            database and which tables the app defines.
Why needed: `alembic upgrade head` (run by the launchers) executes this file to
            build the real tables; without it there would be no database schema.
Contains:
    target_metadata         - the tables Alembic compares against (`Base.metadata`).
    run_migrations_offline  - writes the SQL without connecting (`--sql`).
    do_run_migrations       - applies pending migrations in one transaction.
    run_migrations_online   - opens the async engine and runs them for real.
    The bottom `if` picks offline or online mode automatically.
Used by:    Alembic itself (via alembic.ini); no Python module imports it.
Depends on: auth/__init__ (importing the models registers them), common/config
            (database URL) and common/db (Base, build_engine).
"""

import asyncio  # run the async engine's migrations
from logging.config import fileConfig  # apply alembic.ini's logging setup

from alembic import context  # Alembic's runtime context (offline/online mode)
from sqlalchemy.engine import Connection  # sync-style connection handed to Alembic

# Importing the models registers them on Base.metadata for autogenerate.
from services.auth import models as auth_models  # noqa: F401
from services.common.config import settings
from services.common.db import Base, build_engine

# Alembic's config object, which exposes the values in alembic.ini.
config = context.config
if config.config_file_name is not None:
    fileConfig(config.config_file_name)

# The metadata Alembic diffs against when autogenerating migrations.
target_metadata = Base.metadata


def run_migrations_offline() -> None:
    """Emit migration SQL without connecting to the database (`alembic upgrade --sql`)."""
    # Configure with just a URL: no connection, SQL values written inline.
    context.configure(
        url=settings.sqlalchemy_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
    )
    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    """Run all pending migrations inside one transaction on the given connection."""
    # One transaction: a failed migration leaves the database untouched.
    context.configure(connection=connection, target_metadata=target_metadata, compare_type=True)
    with context.begin_transaction():
        context.run_migrations()


async def run_migrations_online() -> None:
    """Connect with the async engine and run migrations against the live database."""
    # A private engine, disposed afterwards so the process can exit cleanly.
    engine = build_engine()
    async with engine.connect() as connection:
        # Alembic's API is synchronous, so hand it the connection via run_sync.
        await connection.run_sync(do_run_migrations)
    await engine.dispose()


# Alembic runs this file top to bottom; choose the mode it was started in.
if context.is_offline_mode():
    run_migrations_offline()
else:
    asyncio.run(run_migrations_online())
