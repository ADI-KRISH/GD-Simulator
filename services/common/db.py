"""Async SQLAlchemy engine, session factory and declarative base."""

from collections.abc import AsyncIterator

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


def build_engine(url: str | None = None) -> AsyncEngine:
    target = url or settings.sqlalchemy_url
    kwargs: dict = {"echo": False, "future": True}
    if not target.startswith("sqlite"):
        kwargs.update(pool_pre_ping=True, pool_size=10, max_overflow=20)
    return create_async_engine(target, **kwargs)


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
