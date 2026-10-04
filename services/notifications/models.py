"""Notification records (FR-32).

Durable rather than fire-and-forget. The WebSocket control plane already pushes
lifecycle events to whoever is connected, but FR-32 covers things a user needs
to learn about when they are *not* connected — chiefly that their evaluation
finished and a report is waiting. A row survives that gap; a broadcast does not.

There is no email or push transport, and that is deliberate: nothing in this
project is hosted, so there is no mail service to deliver to (the same reason
the password-reset flow returns its token locally instead of mailing it).
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import DateTime, ForeignKey, String, Text, Uuid, func
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from services.common.db import Base


class NotificationKind(StrEnum):
    """The FR-32 list. Wire values — do not rename."""

    LOBBY_CREATED = "lobby_created"
    SESSION_INVITATION = "session_invitation"
    SESSION_STARTING = "session_starting"
    SESSION_ENDED = "session_ended"
    EVALUATION_COMPLETED = "evaluation_completed"
    REPORT_AVAILABLE = "report_available"


class Notification(Base):
    __tablename__ = "notifications"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    session_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("gd_sessions.id", ondelete="CASCADE"), index=True, nullable=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    # Where the notification points — a lobby, room or report path.
    link: Mapped[str | None] = mapped_column(String(300), nullable=True)
    payload: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)

    read_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), index=True, nullable=False
    )
