"""Notification records (FR-32).

Durable rather than fire-and-forget. The WebSocket control plane already pushes
lifecycle events to whoever is connected, but FR-32 covers things a user needs
to learn about when they are *not* connected — chiefly that their evaluation
finished and a report is waiting. A row survives that gap; a broadcast does not.

There is no email or push transport, and that is deliberate: nothing in this
project is hosted, so there is no mail service to deliver to (the same reason
the password-reset flow returns its token locally instead of mailing it).

Purpose: The notifications table and the list of notification kinds.
Why needed: A stored row survives a user being disconnected, so they still see that their lobby,
    invitation or report is ready. There is deliberately no email or push, since nothing in this
    project is hosted.
Contains:
  - NotificationKind: the FR-32 kinds (lobby created, invitation, starting, ended, evaluation
    completed, report available). Wire values, do not rename.
  - Notification: one in-app notification for one user, optionally tied to a session, with a
    link, payload and read time.
Used by: services/evaluation/service.py, services/notifications/router.py,
    services/notifications/service.py, services/session/service.py
Depends on: services/common/db.py
"""

import uuid  # UUID primary keys
from datetime import datetime  # timestamp column types
from enum import StrEnum  # string enum stored as text

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid, func  # column types
from sqlalchemy.orm import Mapped, mapped_column  # typed ORM declarations
from sqlalchemy.types import JSON  # free-form payload column

from services.common.db import Base  # declarative base


class NotificationKind(StrEnum):
    """The FR-32 list. Wire values — do not rename."""

    LOBBY_CREATED = "lobby_created"
    SESSION_INVITATION = "session_invitation"
    SESSION_STARTING = "session_starting"
    SESSION_ENDED = "session_ended"
    EVALUATION_COMPLETED = "evaluation_completed"
    REPORT_AVAILABLE = "report_available"


class Notification(Base):
    """One in-app notification for one user (optionally tied to a session)."""

    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Optional: set when the notification concerns a particular session.
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("gd_sessions.id", ondelete="CASCADE"), index=True, nullable=True
    )
    # A NotificationKind value stored as text, so the UI can pick an icon or action.
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Where the notification points — a lobby, room or report path.
    link: Mapped[str | None] = mapped_column(String(300), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    # NULL means unread; set once when the user opens it.
    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Indexed because lists are shown newest first.
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )
