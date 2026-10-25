"""Notification HTTP API (FR-32).

Purpose: HTTP endpoints under /api/v1/me/notifications: list the caller's notifications, mark
    one read, mark all read.
Why needed: The frontend's notification bell and page read their data here, and every endpoint
    is scoped to the signed-in user.
Contains:
  - NotificationOut / NotificationList: response shapes, with the unread count for the whole
    inbox.
  - list_notifications: newest first, optionally unread only.
  - mark_read: mark one notification read; foreign ids answer 404.
  - mark_all_read: mark every unread notification read and report how many changed.
Used by: services/main.py
Depends on: services/auth/deps.py, services/common/errors.py,
    services/notifications/__init__.py, services/notifications/models.py,
    services/notifications/service.py
"""

import uuid  # notification id path parameter
from datetime import datetime  # timestamps in responses
from typing import Annotated  # attach Query validation to parameters

from fastapi import APIRouter, Query, status  # router, query params, HTTP codes
from pydantic import BaseModel, ConfigDict  # response models

from services.auth.deps import CurrentUser, SessionDep  # injected user and DB session
from services.common.errors import NotFoundError  # 404 for unknown/foreign ids
from services.notifications import service  # notification business logic
from services.notifications.models import NotificationKind  # typed kind in responses

router = APIRouter(prefix="/api/v1/me/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
    """A notification as returned to the client."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    session_id: uuid.UUID | None
    kind: NotificationKind
    title: str
    body: str
    link: str | None
    payload: dict
    read_at: datetime | None
    created_at: datetime


class NotificationList(BaseModel):
    """A page of notifications plus the user's total unread count."""

    unread: int
    items: list[NotificationOut]


@router.get("", response_model=NotificationList)
async def list_notifications(
    db: SessionDep,
    user: CurrentUser,
    unread_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> NotificationList:
    """List the caller's notifications, newest first."""
    # Fetch the caller's page of notifications.
    items = await service.list_for_user(db, user.id, unread_only=unread_only, limit=limit)
    # The unread count covers the whole inbox, not just this page.
    return NotificationList(
        unread=await service.unread_count(db, user.id),
        items=[NotificationOut.model_validate(n) for n in items],
    )


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_read(
    notification_id: uuid.UUID, db: SessionDep, user: CurrentUser
) -> NotificationOut:
    """Mark one notification read."""
    # The service only finds notifications owned by the caller.
    notification = await service.mark_read(db, user.id, notification_id)
    if notification is None:
        # Also the answer when the id belongs to someone else: not found rather
        # than forbidden, so the endpoint cannot confirm another user's ids.
        raise NotFoundError("notification not found")
    return NotificationOut.model_validate(notification)


@router.post("/read-all", status_code=status.HTTP_200_OK)
async def mark_all_read(db: SessionDep, user: CurrentUser) -> dict:
    """Mark every unread notification read and report how many changed."""
    # Report how many were actually changed.
    return {"marked_read": await service.mark_all_read(db, user.id)}
