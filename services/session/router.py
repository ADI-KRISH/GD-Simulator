"""Session HTTP API (FR-03, FR-08, FR-10, FR-11, design §7.5)."""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from services.auth.deps import CurrentUser, SessionDep, require_role
from services.auth.models import Role
from services.common.config import settings
from services.common.errors import ConflictError, PermissionDeniedError
from services.realtime.hub import hub
from services.realtime.livekit import MediaGrant, create_access_token, room_name
from services.realtime.protocol import ServerEvent, server_frame
from services.session import repository as repo
from services.session import service
from services.session.schemas import (
    MediaToken,
    SessionCreate,
    SessionOut,
    SessionSummary,
    SessionUpdate,
    TimerView,
    TopicView,
)
from services.session.state import ParticipantState, SessionState

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])

HostOrAdmin = Annotated[object, Depends(require_role(Role.HOST, Role.ADMIN))]


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(
    payload: SessionCreate, db: SessionDep, user: CurrentUser, _: HostOrAdmin
) -> SessionOut:
    """FR-03. Hosts and admins only."""
    gd = await service.create_session(db, user, payload)
    return SessionOut.model_validate(gd)


@router.get("", response_model=list[SessionSummary])
async def list_my_sessions(
    db: SessionDep,
    user: CurrentUser,
    role: Annotated[str, Query(pattern="^(host|participant)$")] = "participant",
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> list[SessionSummary]:
    if role == "host":
        sessions = await repo.list_sessions_for_host(db, user.id, limit, offset)
    else:
        sessions = await repo.list_sessions_for_participant(db, user.id, limit, offset)
    return [SessionSummary.model_validate(s) for s in sessions]


@router.get("/{session_id}", response_model=SessionOut)
async def get_session(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> SessionOut:
    gd = await service.require_session(db, session_id)
    if gd.host_id != user.id:
        await service.require_participant(db, session_id, user)
    # Reads advance the clock so the lifecycle is right even with no ticker.
    await service.advance_clock(db, gd)
    return SessionOut.model_validate(gd)


@router.patch("/{session_id}", response_model=SessionOut)
async def update_session(
    session_id: uuid.UUID, payload: SessionUpdate, db: SessionDep, user: CurrentUser
) -> SessionOut:
    gd = await service.require_session(db, session_id)
    service.require_host(gd, user)
    await service.update_session(db, gd, payload)
    return SessionOut.model_validate(gd)


@router.post("/{session_id}/start", response_model=SessionOut)
async def start_session(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> SessionOut:
    """Begins preparation (FR-10). The discussion starts when prep elapses."""
    gd = await service.require_session(db, session_id)
    service.require_host(gd, user)
    await service.start_session(db, gd)
    # Commit before telling anyone: a client that hears SESSION_STARTED asks for
    # a media token immediately, and must not see the pre-start state.
    await db.commit()
    phase = "discussion" if SessionState(gd.state) is SessionState.LIVE else "preparation"
    await hub.broadcast(
        str(gd.id), server_frame(ServerEvent.SESSION_STARTED, str(gd.id), {"phase": phase})
    )
    return SessionOut.model_validate(gd)


@router.post("/{session_id}/end", response_model=SessionOut)
async def end_session(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> SessionOut:
    """Ends the discussion early. Evaluation is requested downstream (design §18)."""
    gd = await service.require_session(db, session_id)
    service.require_host(gd, user)
    await service.end_session(db, gd, reason="host_ended")
    await db.commit()
    await hub.broadcast(
        str(gd.id), server_frame(ServerEvent.SESSION_ENDED, str(gd.id), {"reason": "host_ended"})
    )
    return SessionOut.model_validate(gd)


@router.get("/{session_id}/timer", response_model=TimerView)
async def get_timer(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> TimerView:
    """FR-10. Polling fallback for clients whose WebSocket has dropped."""
    gd = await service.require_session(db, session_id)
    await service.require_member(db, gd, user)
    await service.advance_clock(db, gd)
    return service.timer_view(gd)


@router.get("/{session_id}/topic", response_model=TopicView)
async def get_topic(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> TopicView:
    """FR-11."""
    gd = await service.require_session(db, session_id)
    await service.require_member(db, gd, user)
    await service.advance_clock(db, gd)
    return service.topic_view(gd)


@router.get("/{session_id}/media-token", response_model=MediaToken)
async def get_media_token(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> MediaToken:
    """FR-08. Credentials for the LiveKit media plane.

    Issued to a participant who has not left, or to the host as a watcher, and
    only once the session is past the lobby — there is nothing to join before
    that.
    """
    gd = await service.require_session(db, session_id)
    participant = await service.require_member(db, gd, user)
    if participant is not None and ParticipantState(participant.state) is ParticipantState.LEFT:
        raise PermissionDeniedError("you have left this session")
    await service.advance_clock(db, gd)
    if SessionState(gd.state) not in {SessionState.STARTING, SessionState.LIVE}:
        raise ConflictError("the media room is not open for this session yet")

    room = room_name(gd.id)
    # A host who did not join as a candidate watches without publishing: their
    # microphone is never recorded, so anything they said could not appear in
    # the transcript the evaluation is built from.
    identity = participant.candidate_label if participant else service.OBSERVER_LABEL
    token = create_access_token(
        MediaGrant(
            room=room,
            # Identity is the candidate label, so LiveKit track identities line
            # up with transcript speakers without leaking real names.
            identity=identity,
            display_name=participant.display_name if participant else user.full_name,
            can_publish=participant is not None,
            can_subscribe=True,
        )
    )
    return MediaToken(url=settings.livekit_url, token=token, room=room, identity=identity)
