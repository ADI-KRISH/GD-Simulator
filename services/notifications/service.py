"""Notification creation and delivery (FR-32).

Two channels, one call: a durable row, plus a live push to anyone currently on
the WebSocket. Neither can fail the caller — a notification is an inform, never
a step in a transaction that matters, so failures here are logged and swallowed
(the same reasoning as the event bus in `common/events.py`).
"""

import logging
import uuid

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from services.common.timeutil import utcnow
from services.notifications.models import Notification, NotificationKind
from services.realtime.hub import hub
from services.realtime.protocol import ServerEvent, server_frame

logger = logging.getLogger(__name__)


async def notify(
    session: AsyncSession,
    user_ids: list[uuid.UUID],
    kind: NotificationKind,
    title: str,
    body: str,
    *,
    session_id: uuid.UUID | None = None,
    link: str | None = None,
    payload: dict | None = None,
) -> list[Notification]:
    """Create one notification per user and push it to connected clients."""
    created: list[Notification] = []
    for user_id in {u for u in user_ids if u is not None}:
        notification = Notification(
            user_id=user_id,
            session_id=session_id,
            kind=str(kind),
            title=title,
            body=body,
            link=link,
            payload=payload or {},
        )
        session.add(notification)
        created.append(notification)

    if not created:
        return []
    await session.flush()

    if session_id is not None:
        try:
            await hub.broadcast(
                str(session_id),
                server_frame(
                    ServerEvent.NOTIFICATION,
                    str(session_id),
                    {"kind": str(kind), "title": title, "body": body, "link": link},
                ),
            )
        except Exception as exc:  # pragma: no cover - defensive
            logger.debug("notification push failed", extra={"error": str(exc)})

    return created


async def list_for_user(
    session: AsyncSession, user_id: uuid.UUID, *, unread_only: bool = False, limit: int = 50
) -> list[Notification]:
    query = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    result = await session.execute(query.order_by(Notification.created_at.desc()).limit(limit))
    return list(result.scalars())


async def unread_count(session: AsyncSession, user_id: uuid.UUID) -> int:
    result = await session.execute(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
    )
    return int(result.scalar_one())


async def mark_read(
    session: AsyncSession, user_id: uuid.UUID, notification_id: uuid.UUID
) -> Notification | None:
    notification = await session.get(Notification, notification_id)
    # Scoped to the owner: an id from someone else's list must not be readable.
    if notification is None or notification.user_id != user_id:
        return None
    if notification.read_at is None:
        notification.read_at = utcnow()
        await session.flush()
    return notification


async def mark_all_read(session: AsyncSession, user_id: uuid.UUID) -> int:
    unread = await list_for_user(session, user_id, unread_only=True, limit=500)
    now = utcnow()
    for notification in unread:
        notification.read_at = now
    await session.flush()
    return len(unread)
