"""Async SQLAlchemy engine, session factory and declarative base.

Purpose:    build the database engine (Postgres, or SQLite in no-Docker/test
            mode), the session factory, and the base class for all ORM models.
Why needed: every table in the app is defined against `Base`, and every request
            or background loop needs a session; a single module guarantees they
            all share one engine, one transaction policy and the SQLite fixes.
Contains:
    Base                  - declarative base every model inherits (Alembic reads it).
    SQLITE_BUSY_TIMEOUT_MS- how long a SQLite writer waits for the lock.
    _sqlite_pragmas       - turns on WAL + busy timeout so reads don't block commits.
    build_engine          - makes the engine (pooled for Postgres, pragmas for SQLite).
    engine, SessionFactory- the shared engine and session maker.
    get_session           - FastAPI dependency: one commit/rollback session per request.
    get_session_factory   - for long-lived callers (WebSocket, workers) that open
                            their own short sessions.
Used by:    auth/deps and models, evaluation/models and worker, main,
            migrations/env, notifications/models, observer/service,
            practice/router, realtime/timer and ws, session/models, simulation,
            speech/models, tests/conftest and test_realtime_ws.
Depends on: common/config (database URL).
"""

from collections.abc import AsyncIterator  # return type of the session dependency

from sqlalchemy import event  # hook used to set SQLite pragmas on each connection
from sqlalchemy.ext.asyncio import (
    AsyncEngine,  # async database engine type
    AsyncSession,  # async unit-of-work session
    async_sessionmaker,  # factory that builds AsyncSession objects
    create_async_engine,  # builds the engine from a URL
)
from sqlalchemy.orm import DeclarativeBase  # base for all ORM models

from services.common.config import settings  # provides the database URL


class Base(DeclarativeBase):
    """Declarative base every ORM model inherits from (also used by Alembic)."""


#: How long a SQLite writer waits for another to finish before giving up.
SQLITE_BUSY_TIMEOUT_MS = 30_000


def _sqlite_pragmas(dbapi_connection, _record) -> None:
    """WAL, so readers never block a writer; and a writer that waits, not fails.

    The no-Docker mode runs on SQLite, where the default rollback journal lets a
    single open read block every commit, and a busy database fails a write after
    five seconds. Both showed up as 500s while a report was being made.
    """
    # Apply both settings on every new connection, then release the cursor.
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    cursor.close()


def build_engine(url: str | None = None) -> AsyncEngine:
    """Create an async engine; Postgres gets a connection pool, SQLite gets WAL pragmas."""
    # An explicit URL (tests) wins over the configured one.
    target = url or settings.sqlalchemy_url
    kwargs: dict = {"echo": False, "future": True}
    if not target.startswith("sqlite"):
        # pre_ping drops dead pooled connections instead of failing a request.
        kwargs.update(pool_pre_ping=True, pool_size=10, max_overflow=20)
    built = create_async_engine(target, **kwargs)
    # SQLite only: install the WAL/busy-timeout hook on each new connection.
    if target.startswith("sqlite"):
        event.listen(built.sync_engine, "connect", _sqlite_pragmas)
    return built


# Module-level engine and session factory shared by the whole app.
engine: AsyncEngine = build_engine()
SessionFactory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one transaction-scoped session per request."""
    async with SessionFactory() as session:
        try:
            yield session
            # Commit only if the request handler finished without raising.
            await session.commit()
        except Exception:
            # Undo partial writes so a failed request leaves no half-saved state.
            await session.rollback()
            raise


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """FastAPI dependency for callers that need to open their own short-lived
    sessions — the WebSocket handler and background workers, whose lifetime is
    far longer than a single transaction should be.
    """
    return SessionFactory
