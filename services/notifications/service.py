"""Notification creation and delivery (FR-32).

Two channels, one call: a durable row, plus a live push to anyone currently on
the WebSocket. Neither can fail the caller — a notification is an inform, never
a step in a transaction that matters, so failures here are logged and swallowed
(the same reasoning as the event bus in `common/events.py`).

Purpose: Creates notifications (a durable row plus a live push to anyone connected to the
    session) and reads and marks them for a user.
Why needed: Other slices call notify() when something a user cares about happens. A notification
    is informational, so failures are swallowed and can never break the transaction that
    triggered it.
Contains:
  - notify: create one notification per user and push it live.
  - list_for_user: a user's notifications, newest first, optionally unread only.
  - unread_count: how many the user has not read.
  - mark_read: mark one of the user's own notifications read.
  - mark_all_read: mark up to 500 unread notifications read.
Used by: services/evaluation/service.py, services/notifications/router.py,
    services/session/service.py
Depends on: services/common/timeutil.py, services/notifications/models.py,
    services/realtime/hub.py, services/realtime/protocol.py
"""

import logging  # debug-log failed pushes
import uuid  # user/session id types

from sqlalchemy import func, select  # count() and queries
from sqlalchemy.ext.asyncio import AsyncSession  # database session

from services.common.timeutil import utcnow  # read_at timestamps
from services.notifications.models import Notification, NotificationKind  # table and kinds
from services.realtime.hub import hub  # live push to connected clients
from services.realtime.protocol import ServerEvent, server_frame  # WebSocket event frames

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
    # A set removes duplicates and None, so each user gets one notification.
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

    # Nobody to notify.
    if not created:
        return []
    # Flush so rows get ids inside the caller's transaction (the caller commits).
    await session.flush()

    # Also push live to anyone connected to this session's room.
    if session_id is not None:
        # A failed push must never fail the caller; the stored row is the durable copy.
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
    """A user's notifications, newest first, optionally only the unread ones."""
    # Scope to the owner first, so users only see their own notifications.
    query = select(Notification).where(Notification.user_id == user_id)
    if unread_only:
        query = query.where(Notification.read_at.is_(None))
    # Newest first, capped by limit.
    result = await session.execute(query.order_by(Notification.created_at.desc()).limit(limit))
    return list(result.scalars())


async def unread_count(session: AsyncSession, user_id: uuid.UUID) -> int:
    """How many notifications the user has not read yet."""
    # Count in SQL instead of loading rows.
    result = await session.execute(
        select(func.count())
        .select_from(Notification)
        .where(Notification.user_id == user_id, Notification.read_at.is_(None))
    )
    return int(result.scalar_one())


async def mark_read(
    session: AsyncSession, user_id: uuid.UUID, notification_id: uuid.UUID
) -> Notification | None:
    """Mark one of the user's notifications read; None if it is not theirs or missing."""
    notification = await session.get(Notification, notification_id)
    # Scoped to the owner: an id from someone else's list must not be readable.
    if notification is None or notification.user_id != user_id:
        return None
    # Only stamp the first read, so re-reading keeps the original time.
    if notification.read_at is None:
        notification.read_at = utcnow()
        await session.flush()
    return notification


async def mark_all_read(session: AsyncSession, user_id: uuid.UUID) -> int:
    """Mark up to 500 unread notifications read; return how many were marked."""
    # A bounded batch: marks at most 500 at once.
    unread = await list_for_user(session, user_id, unread_only=True, limit=500)
    now = utcnow()
    for notification in unread:
        notification.read_at = now
    await session.flush()
    return len(unread)
