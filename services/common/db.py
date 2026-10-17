"""Async SQLAlchemy engine, session factory and declarative base."""

from collections.abc import AsyncIterator

from sqlalchemy import event
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)
from sqlalchemy.orm import DeclarativeBase

from services.common.config import settings


class Base(DeclarativeBase):
    pass


#: How long a SQLite writer waits for another to finish before giving up.
SQLITE_BUSY_TIMEOUT_MS = 30_000


def _sqlite_pragmas(dbapi_connection, _record) -> None:
    """WAL, so readers never block a writer; and a writer that waits, not fails.

    The no-Docker mode runs on SQLite, where the default rollback journal lets a
    single open read block every commit, and a busy database fails a write after
    five seconds. Both showed up as 500s while a report was being made.
    """
    cursor = dbapi_connection.cursor()
    cursor.execute("PRAGMA journal_mode=WAL")
    cursor.execute(f"PRAGMA busy_timeout={SQLITE_BUSY_TIMEOUT_MS}")
    cursor.close()


def build_engine(url: str | None = None) -> AsyncEngine:
    target = url or settings.sqlalchemy_url
    kwargs: dict = {"echo": False, "future": True}
    if not target.startswith("sqlite"):
        kwargs.update(pool_pre_ping=True, pool_size=10, max_overflow=20)
    built = create_async_engine(target, **kwargs)
    if target.startswith("sqlite"):
        event.listen(built.sync_engine, "connect", _sqlite_pragmas)
    return built


engine: AsyncEngine = build_engine()
SessionFactory = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


async def get_session() -> AsyncIterator[AsyncSession]:
    """FastAPI dependency: one transaction-scoped session per request."""
    async with SessionFactory() as session:
        try:
            yield session
            await session.commit()
        except Exception:
            await session.rollback()
            raise


def get_session_factory() -> async_sessionmaker[AsyncSession]:
    """FastAPI dependency for callers that need to open their own short-lived
    sessions — the WebSocket handler and background workers, whose lifetime is
    far longer than a single transaction should be.
    """
    return SessionFactory
