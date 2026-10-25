"""Session HTTP API (FR-03, FR-08, FR-10, FR-11, design §7.5).

Purpose: The HTTP endpoints under /api/v1/sessions: create, list, read and edit a session, start
    and end it, and fetch its timer, topic and LiveKit media token.
Why needed: It is how the frontend drives a discussion through its lifecycle; routes stay thin,
    call service.py for the rules and broadcast the resulting events to the websocket room.
Contains:
  - create_session: open a new lobby (FR-03).
  - list_my_sessions: the caller's sessions as host or as participant.
  - get_session / update_session: read a session, or let the host edit it before it starts.
  - start_session / end_session: host actions that move the lifecycle and announce it to the
    room.
  - get_timer: polling fallback for the server-authoritative clock (FR-10).
  - get_topic: the topic, withheld until preparation begins (FR-11).
  - get_media_token: LiveKit credentials once the room is open (FR-08).
Used by: services/main.py
Depends on: services/auth/deps.py, services/common/config.py, services/common/errors.py,
    services/realtime/hub.py, services/realtime/livekit.py, services/realtime/protocol.py,
    services/session/__init__.py, services/session/schemas.py, services/session/service.py,
    services/session/state.py
"""

import uuid  # session id path parameter
from typing import Annotated  # attach Query validation to parameters

from fastapi import APIRouter, Query, status  # router, query params, HTTP codes

from services.auth.deps import CurrentUser, SessionDep  # injected user and DB session
from services.common.config import settings  # LiveKit URL for media tokens
from services.common.errors import ConflictError, PermissionDeniedError  # 409 / 403
from services.realtime.hub import hub  # broadcast live events to connected clients
from services.realtime.livekit import MediaGrant, create_access_token, room_name  # LiveKit JWTs
from services.realtime.protocol import ServerEvent, server_frame  # WebSocket event frames
from services.session import repository as repo  # data access
from services.session import service  # business rules
from services.session.schemas import (  # request/response shapes
    MediaToken,
    SessionCreate,
    SessionOut,
    SessionSummary,
    SessionUpdate,
    TimerView,
    TopicView,
)
from services.session.state import ParticipantState, SessionState  # state checks

router = APIRouter(prefix="/api/v1/sessions", tags=["sessions"])


@router.post("", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_session(payload: SessionCreate, db: SessionDep, user: CurrentUser) -> SessionOut:
    """FR-03. Any signed-in user can open a lobby; a student creator takes seat A."""
    # The service validates, generates the join code and seats a student creator as candidate A.
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
    """List the caller's sessions, either as host or as participant (paginated)."""
    # The same endpoint serves a host's hosted sessions and a participant's history.
    if role == "host":
        sessions = await repo.list_sessions_for_host(db, user.id, limit, offset)
    else:
        sessions = await repo.list_sessions_for_participant(db, user.id, limit, offset)
    return [SessionSummary.model_validate(s) for s in sessions]


@router.get("/{session_id}", response_model=SessionOut)
async def get_session(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> SessionOut:
    """Return one session; only its host or a participant may read it."""
    # 404 if the session does not exist.
    gd =await service.require_session(db, session_id)
    # The host may read it; anyone else must hold a seat.
    if gd.host_id != user.id:
        await service.require_participant(db, session_id, user)
    # Reads advance the clock so the lifecycle is right even with no ticker.
    await service.advance_clock(db, gd)
    return SessionOut.model_validate(gd)


@router.patch("/{session_id}", response_model=SessionOut)
async def update_session(
    session_id: uuid.UUID, payload: SessionUpdate, db: SessionDep, user: CurrentUser
) -> SessionOut:
    """Host-only edit of session settings before it starts."""
    gd = await service.require_session(db, session_id)
    # Only the host (or an admin) may edit.
    service.require_host(gd, user)
    # The service refuses edits once the lobby has closed.
    await service.update_session(db, gd, payload)
    return SessionOut.model_validate(gd)


@router.post("/{session_id}/start", response_model=SessionOut)
async def start_session(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> SessionOut:
    """Begins preparation (FR-10). The discussion starts when prep elapses."""
    gd = await service.require_session(db, session_id)
    # Only the host can start the session.
    service.require_host(gd, user)
    # Re-checks the start condition and moves READY to STARTING (preparation begins).
    await service.start_session(db, gd)
    # Commit before telling anyone: a client that hears SESSION_STARTED asks for
    # a media token immediately, and must not see the pre-start state.
    await db.commit()
    # A zero-length preparation goes straight to LIVE, so report the phase we are really in.
    phase = "discussion" if SessionState(gd.state) is SessionState.LIVE else "preparation"
    # Push SESSION_STARTED over the websocket to everyone already in the room.
    await hub.broadcast(
        str(gd.id), server_frame(ServerEvent.SESSION_STARTED, str(gd.id), {"phase": phase})
    )
    return SessionOut.model_validate(gd)


@router.post("/{session_id}/end", response_model=SessionOut)
async def end_session(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> SessionOut:
    """Ends the discussion early. Evaluation is requested downstream (design §18)."""
    gd = await service.require_session(db, session_id)
    # Only the host can end the session early.
    service.require_host(gd, user)
    # LIVE to ENDING to ENDED; evaluation is requested downstream by an event, not called here.
    await service.end_session(db, gd, reason="host_ended")
    # Commit first so clients that hear SESSION_ENDED see the final state.
    await db.commit()
    # Tell the room the discussion is over and why.
    await hub.broadcast(
        str(gd.id), server_frame(ServerEvent.SESSION_ENDED, str(gd.id), {"reason": "host_ended"})
    )
    return SessionOut.model_validate(gd)


@router.get("/{session_id}/timer", response_model=TimerView)
async def get_timer(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> TimerView:
    """FR-10. Polling fallback for clients whose WebSocket has dropped."""
    gd = await service.require_session(db, session_id)
    # Participants and the host (as an observer) may read the clock.
    await service.require_member(db, gd, user)
    # Apply any phase change the clock says is due before answering.
    await service.advance_clock(db, gd)
    return service.timer_view(gd)


@router.get("/{session_id}/topic", response_model=TopicView)
async def get_topic(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> TopicView:
    """FR-11."""
    gd = await service.require_session(db, session_id)
    await service.require_member(db, gd, user)
    await service.advance_clock(db, gd)
    # The topic is empty until the session leaves the lobby (see topic_view).
    return service.topic_view(gd)


@router.get("/{session_id}/media-token", response_model=MediaToken)
async def get_media_token(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> MediaToken:
    """FR-08. Credentials for the LiveKit media plane.

    Issued to a participant who has not left, or to the host as a watcher, and
    only once the session is past the lobby — there is nothing to join before
    that.
    """
    gd = await service.require_session(db, session_id)
    # None means the host observing without a seat.
    participant = await service.require_member(db, gd, user)
    # Someone who left cannot rejoin the media room.
    if participant is not None and ParticipantState(participant.state) is ParticipantState.LEFT:
        raise PermissionDeniedError("you have left this session")
    # Make sure the state is current before deciding whether the room is open.
    await service.advance_clock(db, gd)
    # Media credentials only exist once preparation has begun.
    if SessionState(gd.state) not in {SessionState.STARTING, SessionState.LIVE}:
        raise ConflictError("the media room is not open for this session yet")

    # One LiveKit room per session, named from the session id.
    room = room_name(gd.id)
    # A host who did not join as a candidate watches without publishing: their
    # microphone is never recorded, so anything they said could not appear in
    # the transcript the evaluation is built from.
    identity = participant.candidate_label if participant else service.OBSERVER_LABEL
    # Mint the LiveKit JWT locally; no call to the media server is needed.
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
    # The browser connects to the LiveKit url with this token.
    return MediaToken(url=settings.livekit_url, token=token, room=room, identity=identity)
