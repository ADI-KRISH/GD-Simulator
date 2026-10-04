"""Lobby HTTP API (FR-04 – FR-07, design §7.4).

The lobby is a view over the session plus its participants rather than a table
of its own: one lobby per session, created implicitly when the session is. That
keeps membership in a single durable place instead of splitting it across two
records that could disagree.
"""

import uuid

from fastapi import APIRouter, status

from services.auth.deps import CurrentUser, SessionDep
from services.realtime.hub import hub
from services.realtime.protocol import ServerEvent, server_frame
from services.session import service
from services.session.schemas import (
    InviteRequest,
    JoinRequest,
    LobbyView,
    ParticipantOut,
    ReadyRequest,
)

router = APIRouter(prefix="/api/v1/lobbies", tags=["lobby"])


@router.post("/join", response_model=LobbyView)
async def join_lobby(payload: JoinRequest, db: SessionDep, user: CurrentUser) -> LobbyView:
    """FR-05. Join by session code; re-joining is idempotent."""
    gd, participant = await service.join_by_code(db, user, payload.code)
    view = await service.lobby_view(db, gd)
    await hub.broadcast(
        str(gd.id),
        server_frame(
            ServerEvent.USER_JOINED,
            str(gd.id),
            {
                "candidate_label": participant.candidate_label,
                "display_name": participant.display_name,
                "occupancy": view.occupancy,
            },
        ),
        exclude_user=str(user.id),
    )
    return view


@router.post(
    "/{session_id}/invite", response_model=ParticipantOut, status_code=status.HTTP_201_CREATED
)
async def invite_candidate(
    session_id: uuid.UUID, payload: InviteRequest, db: SessionDep, user: CurrentUser
) -> ParticipantOut:
    """FR-05 (invitation). Host-only; reserves a seat and notifies the invitee."""
    gd = await service.require_session(db, session_id)
    service.require_host(gd, user)
    participant = await service.invite(db, gd, payload.email)
    return ParticipantOut.model_validate(participant)


@router.get("/{session_id}", response_model=LobbyView)
async def get_lobby(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> LobbyView:
    """FR-04."""
    gd = await service.require_session(db, session_id)
    if gd.host_id != user.id:
        await service.require_participant(db, session_id, user)
    return await service.lobby_view(db, gd)


@router.post("/{session_id}/ready", response_model=ParticipantOut)
async def set_ready(
    session_id: uuid.UUID, payload: ReadyRequest, db: SessionDep, user: CurrentUser
) -> ParticipantOut:
    """FR-07."""
    gd = await service.require_session(db, session_id)
    participant = await service.set_ready(db, gd, user, payload.ready)
    view = await service.lobby_view(db, gd)
    await hub.broadcast(
        str(gd.id),
        server_frame(
            ServerEvent.USER_READY,
            str(gd.id),
            {
                "candidate_label": participant.candidate_label,
                "ready": payload.ready,
                "ready_count": view.ready_count,
                "start_condition_met": view.start_condition_met,
            },
        ),
    )
    return ParticipantOut.model_validate(participant)


@router.post("/{session_id}/leave", status_code=status.HTTP_204_NO_CONTENT)
async def leave_lobby(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> None:
    """FR-06. Remaining participants are notified."""
    gd = await service.require_session(db, session_id)
    participant = await service.require_participant(db, session_id, user)
    label = participant.candidate_label
    await service.leave(db, gd, user)
    view = await service.lobby_view(db, gd)
    await hub.broadcast(
        str(gd.id),
        server_frame(
            ServerEvent.USER_LEFT,
            str(gd.id),
            {"candidate_label": label, "occupancy": view.occupancy},
        ),
        exclude_user=str(user.id),
    )
