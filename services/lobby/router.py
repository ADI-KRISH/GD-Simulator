"""Lobby HTTP API (FR-04 – FR-07, design §7.4).

The lobby is a view over the session plus its participants rather than a table
of its own: one lobby per session, created implicitly when the session is. That
keeps membership in a single durable place instead of splitting it across two
records that could disagree.

Purpose: HTTP endpoints for the lobby: browse open lobbies, join by code, invite, view, set
    ready and leave.
Why needed: This is how students find and enter a group discussion before it starts; it also
    pushes live join/ready/leave events to everyone already connected.
Contains:
  - browse_lobbies: paginated list of open group lobbies anyone can join.
  - join_lobby: join by the short session code (FR-05) and tell the room.
  - invite_candidate: host-only invitation that reserves a seat.
  - get_lobby: lobby snapshot for the host or a participant (FR-04).
  - set_ready: mark yourself ready or not ready and tell the room (FR-07).
  - leave_lobby: leave the lobby and tell the remaining participants (FR-06).
Used by: services/main.py
Depends on: services/auth/deps.py, services/realtime/hub.py, services/realtime/protocol.py,
    services/session/__init__.py, services/session/schemas.py, services/session/service.py
"""

import uuid  # session id path parameter
from typing import Annotated  # attach Query validation to parameters

from fastapi import APIRouter, Query, status  # router, query params, HTTP codes

from services.auth.deps import CurrentUser, SessionDep  # injected user and DB session
from services.realtime.hub import hub  # push lobby changes to connected clients
from services.realtime.protocol import ServerEvent, server_frame  # WebSocket event frames
from services.session import service  # lobby business rules
from services.session.schemas import (  # request/response shapes
    InviteRequest,
    JoinRequest,
    LobbyListItem,
    LobbyView,
    ParticipantOut,
    ReadyRequest,
)

router = APIRouter(prefix="/api/v1/lobbies", tags=["lobby"])


@router.get("", response_model=list[LobbyListItem])
async def browse_lobbies(
    db: SessionDep,
    user: CurrentUser,
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[LobbyListItem]:
    """Open group lobbies any student can join."""
    # Paginated; limit is capped at 100 so one request cannot dump every lobby.
    return await service.list_open_lobbies(db, limit, offset)


@router.post("/join", response_model=LobbyView)
async def join_lobby(payload: JoinRequest, db: SessionDep, user: CurrentUser) -> LobbyView:
    """FR-05. Join by session code; re-joining is idempotent."""
    # Join by the short session code; joining twice returns the same participant.
    gd, participant = await service.join_by_code(db, user, payload.code)
    # Build the fresh lobby snapshot (occupancy, readiness) to return and to broadcast.
    view = await service.lobby_view(db, gd)
    # Tell everyone already connected that someone joined; the joiner is excluded because they get
    # the HTTP response.
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
    # 404 if the session does not exist.
    gd = await service.require_session(db, session_id)
    # Only the session host may invite.
    service.require_host(gd, user)
    # Reserve a seat for the invitee by email and notify them.
    participant = await service.invite(db, gd, payload.email)
    return ParticipantOut.model_validate(participant)


@router.get("/{session_id}", response_model=LobbyView)
async def get_lobby(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> LobbyView:
    """FR-04."""
    gd = await service.require_session(db, session_id)
    # The host can always look; anyone else must actually be a participant.
    if gd.host_id != user.id:
        await service.require_participant(db, session_id, user)
    return await service.lobby_view(db, gd)


@router.post("/{session_id}/ready", response_model=ParticipantOut)
async def set_ready(
    session_id: uuid.UUID, payload: ReadyRequest, db: SessionDep, user: CurrentUser
) -> ParticipantOut:
    """FR-07."""
    gd = await service.require_session(db, session_id)
    # Record the participant's readiness; the service applies the state rules.
    participant = await service.set_ready(db, gd, user, payload.ready)
    # Re-read the lobby so ready_count and start_condition_met are current.
    view = await service.lobby_view(db, gd)
    # Push the new readiness to every connected client, including the sender, so all screens stay in
    # sync.
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
    # Only members can leave; outsiders get a 403 from require_participant.
    participant = await service.require_participant(db, session_id, user)
    # Capture the label before leaving, because the participant row changes afterwards.
    label = participant.candidate_label
    # Mark the participant as LEFT and free the seat.
    await service.leave(db, gd, user)
    view = await service.lobby_view(db, gd)
    # Tell the remaining participants who left and the new occupancy.
    await hub.broadcast(
        str(gd.id),
        server_frame(
            ServerEvent.USER_LEFT,
            str(gd.id),
            {"candidate_label": label, "occupancy": view.occupancy},
        ),
        exclude_user=str(user.id),
    )
