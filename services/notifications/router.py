"""Notification HTTP API (FR-32)."""

import uuid
from datetime import datetime
from typing import Annotated

from fastapi import APIRouter, Query, status
from pydantic import BaseModel, ConfigDict

from services.auth.deps import CurrentUser, SessionDep
from services.common.errors import NotFoundError
from services.notifications import service
from services.notifications.models import NotificationKind

router = APIRouter(prefix="/api/v1/me/notifications", tags=["notifications"])


class NotificationOut(BaseModel):
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
    unread: int
    items: list[NotificationOut]


@router.get("", response_model=NotificationList)
async def list_notifications(
    db: SessionDep,
    user: CurrentUser,
    unread_only: Annotated[bool, Query()] = False,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
) -> NotificationList:
    items = await service.list_for_user(db, user.id, unread_only=unread_only, limit=limit)
    return NotificationList(
        unread=await service.unread_count(db, user.id),
        items=[NotificationOut.model_validate(n) for n in items],
    )


@router.post("/{notification_id}/read", response_model=NotificationOut)
async def mark_read(
    notification_id: uuid.UUID, db: SessionDep, user: CurrentUser
) -> NotificationOut:
    notification = await service.mark_read(db, user.id, notification_id)
    if notification is None:
        # Also the answer when the id belongs to someone else: not found rather
        # than forbidden, so the endpoint cannot confirm another user's ids.
        raise NotFoundError("notification not found")
    return NotificationOut.model_validate(notification)


@router.post("/read-all", status_code=status.HTTP_200_OK)
async def mark_all_read(db: SessionDep, user: CurrentUser) -> dict:
    return {"marked_read": await service.mark_all_read(db, user.id)}
