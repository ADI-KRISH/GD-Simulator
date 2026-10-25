"""WebSocket control plane (FR-08, FR-09, design §10).

Application and control events only. Audio and video never travel through here
— that is the LiveKit media plane's job. Keeping the two apart is what lets a
media failure and a control failure be handled independently.

A dropped WebSocket marks the participant DISCONNECTED and nothing more: the
session keeps running (design §9, FR-38 candidate disconnect).

Purpose: The WebSocket control-plane endpoint for one participant in one session: authenticates,
    sends a full state snapshot, then handles ready/not-ready, speech start/stop and ping
    events.
Why needed: It keeps browsers in sync in real time (lobby, timer, turns) while audio and video
    stay on LiveKit. A dropped socket only marks the participant DISCONNECTED, so a flaky
    connection never ends the session.
Contains:
  - CLOSE_UNAUTHORIZED / CLOSE_FORBIDDEN / CLOSE_NOT_FOUND: application close codes.
  - _snapshot: everything a connecting or reconnecting client needs in one frame.
  - session_socket: authenticate, join the hub, then run the event loop.
  - broadcast_turns: tell the room about each changed turn with the running interruption
    penalty.
  - _record_disconnect: persist a disconnect and tell the room.
Used by: services/main.py, services/practice/engine.py
Depends on: services/auth/__init__.py, services/common/db.py, services/common/errors.py,
    services/common/security.py, services/realtime/hub.py, services/realtime/protocol.py,
    services/session/__init__.py, services/session/state.py
"""

import logging  # warn when the socket loop fails
import uuid  # session/user id types
from typing import Annotated, Any  # Query/Depends metadata and loose dict types

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect  # WebSocket routing
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # DB session types

from services.auth import repository as auth_repo  # look up the connecting user
from services.common.db import get_session_factory  # open short-lived DB sessions per event
from services.common.errors import DomainError  # expected, user-facing failures
from services.common.security import decode_token  # verify the access token from the URL
from services.realtime.hub import Connection, hub  # room registry and broadcasting
from services.realtime.protocol import ClientEvent, ServerEvent, error_frame, server_frame
from services.session import repository as session_repo  # session/participant lookups
from services.session import service as session_service  # readiness, speech, clocks
from services.session.state import ParticipantState, SessionState  # state checks

logger = logging.getLogger(__name__)

router = APIRouter(tags=["realtime"])

# Injects the session factory (not a session): the socket outlives any one transaction.
SessionFactoryDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]

# Close codes. 4001-4999 is the application-private range.
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404


async def _snapshot(db: AsyncSession, gd, viewer_label: str) -> dict[str, Any]:
    """Everything a (re)connecting client needs in one frame."""
    # Roster and readiness, so the client can draw the lobby without a second request.
    lobby = await session_service.lobby_view(db, gd)
    # Current clock and phase.
    timer = session_service.timer_view(gd)
    # The topic, which is empty until it is released.
    topic = session_service.topic_view(gd)
    # One frame holding the whole picture, used on first connect and on reconnects.
    return {
        "you": viewer_label,
        "session_state": str(gd.state),
        "lobby": lobby.model_dump(mode="json"),
        "timer": timer.model_dump(mode="json"),
        "topic": topic.model_dump(mode="json"),
        "connected": hub.occupancy(str(gd.id)),
    }


@router.websocket("/ws/sessions/{session_id}")
async def session_socket(
    websocket: WebSocket,
    session_id: uuid.UUID,
    factory: SessionFactoryDep,
    token: Annotated[str | None, Query(description="Access token")] = None,
) -> None:
    """Control-plane socket for one participant in one session.

    The token rides in the query string because the browser WebSocket API
    cannot set an Authorization header. It is a short-lived access token, and
    the connection is upgraded only after it verifies.
    """
    # Reject before upgrading: a socket without a token never opens.
    if not token:
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="missing token")
        return

    try:
        # `subject` is already a UUID — `decode_token` parses and validates it.
        user_id = decode_token(token, "access").subject
    # A bad signature, an expired token or the wrong token type all end the same way: close with
    # 4401.
    except (DomainError, ValueError):
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="invalid token")
        return

    # A short transaction for the checks and reconnect bookkeeping, closed before the long socket
    # loop.
    async with factory() as db:
        # Load the account and session before deciding what this connection may do.
        user = await auth_repo.get_user_by_id(db, user_id)
        gd = await session_repo.get_session_by_id(db, session_id)
        # Either one missing: close with 4404.
        if user is None or gd is None:
            await websocket.close(code=CLOSE_NOT_FOUND, reason="session not found")
            return
        # The caller's seat, if they have one.
        participant = await session_repo.get_participant(db, session_id, user_id)
        # A participant who left is treated like a non-participant.
        left = (
            participant is not None and ParticipantState(participant.state) is ParticipantState.LEFT
        )
        # The host watches their own session without being a candidate in it.
        is_observer = participant is None or left
        # Only the host may watch without a seat; everyone else is refused with 4403.
        if is_observer and gd.host_id != user_id:
            await websocket.close(code=CLOSE_FORBIDDEN, reason="not a participant")
            return
        # The identity used in every frame: a candidate label, or HOST for an observer.
        label = session_service.OBSERVER_LABEL if is_observer else participant.candidate_label
        # Re-entering after a drop is a reconnect, not a fresh join (design §9).
        was_disconnected = (
            not is_observer and ParticipantState(participant.state) is ParticipantState.DISCONNECTED
        )
        # Move DISCONNECTED to RECONNECTED to ACTIVE in the database.
        if was_disconnected:
            await session_service.mark_reconnected(db, participant)
        # Build the state frame before closing the database session.
        snapshot = await _snapshot(db, gd, label)
        # Commit the reconnect (if any) before the socket is accepted.
        await db.commit()

    # Accept only after authentication and authorisation have passed.
    await websocket.accept()
    # Register in the hub so broadcasts reach this socket.
    connection = Connection(
        websocket=websocket, session_id=str(session_id), user_id=str(user_id), candidate_label=label
    )
    await hub.connect(connection)

    # First frame: the full current state, so the client never has to replay history.
    await hub.send(connection, server_frame(ServerEvent.STATE_SNAPSHOT, str(session_id), snapshot))
    # An observer is not on the roster, so its arrival is not a roster change.
    if not is_observer:
        await hub.broadcast(
            str(session_id),
            server_frame(
                ServerEvent.RECONNECTED if was_disconnected else ServerEvent.USER_JOINED,
                str(session_id),
                {"candidate_label": label, "display_name": participant.display_name},
            ),
            exclude_user=str(user_id),
        )

    # Wall-clock offset of an in-flight utterance, set on SPEECH_STARTED.
    speaking_since: float | None = None

    try:
        # Main loop: one JSON message per iteration, dispatched on its "type".
        while True:
            # Blocks until the browser sends a JSON message; raises WebSocketDisconnect on close.
            raw = await websocket.receive_json()
            # The dispatch key; unknown or missing types fall through to the error reply at the
            # bottom.
            event = str(raw.get("type", ""))

            # Keep-alive: answer with a pong so the client knows the socket is alive.
            if event == ClientEvent.PING:
                await hub.send(
                    connection,
                    server_frame(ServerEvent.STATE_SNAPSHOT, str(session_id), {"pong": True}),
                )
                continue

            if is_observer and event in {
                ClientEvent.READY,
                ClientEvent.NOT_READY,
                ClientEvent.SPEECH_STARTED,
                ClientEvent.SPEECH_STOPPED,
            }:
                # An observer has no candidate label, so nothing it said could be
                # attributed — readiness and speech belong to candidates only.
                await hub.send(
                    connection,
                    error_frame(
                        str(session_id), "not_a_candidate", "join with the code to take part"
                    ),
                )
                continue

            # Lobby readiness: update the DB, then tell the whole room.
            if event in {ClientEvent.READY, ClientEvent.NOT_READY}:
                ready = event == ClientEvent.READY
                # A fresh database session per event; the socket outlives any single transaction.
                async with factory() as db:
                    gd = await session_repo.get_session_by_id(db, session_id)
                    # Domain rules (for example readiness locked after start) come back as an error
                    # frame, not a crash.
                    try:
                        await session_service.set_ready(db, gd, user, ready)
                        await db.commit()
                    except DomainError as exc:
                        # Undo partial work, tell only this client, and keep the socket open.
                        await db.rollback()
                        await hub.send(
                            connection, error_frame(str(session_id), exc.code, exc.message)
                        )
                        continue
                    # Fresh counts for the broadcast below.
                    lobby = await session_service.lobby_view(db, gd)
                await hub.broadcast(
                    str(session_id),
                    server_frame(
                        ServerEvent.USER_READY,
                        str(session_id),
                        {
                            "candidate_label": label,
                            "ready": ready,
                            "start_condition_met": lobby.start_condition_met,
                            "ready_count": lobby.ready_count,
                        },
                    ),
                )
                continue

            # Speech start: remember when (in discussion time) and tell the room.
            if event == ClientEvent.SPEECH_STARTED:
                async with factory() as db:
                    gd = await session_repo.get_session_by_id(db, session_id)
                    # Speech only counts during the live discussion; ignore it during lobby and
                    # preparation.
                    if SessionState(gd.state) is not SessionState.LIVE:
                        continue
                    # Seconds since LIVE, the same clock as transcript segments.
                    speaking_since = session_service.elapsed_discussion_seconds(gd)
                await hub.broadcast(
                    str(session_id),
                    server_frame(
                        ServerEvent.SPEECH_STARTED,
                        str(session_id),
                        {"candidate_label": label, "start": speaking_since},
                    ),
                )
                continue

            # Speech stop: save the interval, detect interruptions/replies, broadcast turns.
            if event == ClientEvent.SPEECH_STOPPED:
                # A stop without a start is ignored.
                if speaking_since is None:
                    continue
                async with factory() as db:
                    gd = await session_repo.get_session_by_id(db, session_id)
                    # Never let the end precede the start, even if the clocks round.
                    end = max(session_service.elapsed_discussion_seconds(gd), speaking_since)
                    # Persisted immediately — the metrics engine computes
                    # speaking time from these rows, never from an estimate.
                    _, changed = await session_service.record_speech_tracked(
                        db, gd, label, speaking_since, end
                    )
                    # Running interruption counts, used for the live penalty shown in the UI.
                    interrupts = await session_service.interruption_times(db, session_id)
                    await db.commit()
                await hub.broadcast(
                    str(session_id),
                    server_frame(
                        ServerEvent.SPEECH_STOPPED,
                        str(session_id),
                        {"candidate_label": label, "start": speaking_since, "end": end},
                    ),
                )
                # Tell the room about every turn created or newly marked as an interruption.
                await broadcast_turns(str(session_id), changed, interrupts)
                # Ready for the next utterance.
                speaking_since = None
                continue

            # Anything else is not part of the protocol.
            await hub.send(
                connection, error_frame(str(session_id), "unknown_event", f"unsupported: {event}")
            )

    # The normal way a socket ends: the browser closed it.
    except WebSocketDisconnect:
        pass
    # Anything unexpected is logged; the finally block still cleans up.
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "websocket loop failed",
            extra={"session_id": str(session_id), "label": label, "error": str(exc)},
        )
    finally:
        # Always leave the room, however the loop ended.
        await hub.disconnect(connection)
        # Only mark DISCONNECTED if this was the participant's last socket —
        # a second tab closing must not report them as gone.
        if not is_observer and not hub.is_connected(str(session_id), str(user_id)):
            await _record_disconnect(factory, session_id, user_id, label)


async def broadcast_turns(session_id: str, turns, interrupts: dict[str, list[float]]) -> None:
    """Tell the room about every turn that changed, with the running penalty."""
    # Reuses the aggregator's penalty function so the live figure follows the same rule as scoring.
    from services.evaluation.aggregator import interruption_penalty

    # One TURN_RECORDED frame per changed turn.
    for turn in turns:
        # How many interruptions this speaker has made so far.
        made = len(interrupts.get(turn.candidate_label, []))
        await hub.broadcast(
            session_id,
            server_frame(
                ServerEvent.TURN_RECORDED,
                session_id,
                {
                    "candidate_label": turn.candidate_label,
                    "start": turn.start,
                    "end": turn.end,
                    "interrupted": turn.interrupted_label,
                    "reply_to": turn.reply_to_label,
                    "interruptions": made,
                    "penalty": interruption_penalty(made),
                },
            ),
        )


async def _record_disconnect(
    factory: async_sessionmaker[AsyncSession],
    session_id: uuid.UUID,
    user_id: uuid.UUID,
    label: str,
) -> None:
    """Persist the disconnect and tell the room (design §9, FR-09)."""
    async with factory() as db:
        # Re-load inside a fresh session; the socket's earlier one is closed.
        participant = await session_repo.get_participant(db, session_id, user_id)
        if participant is None:
            return
        # A DomainError (for example already LEFT) means there is nothing to record.
        try:
            await session_service.mark_disconnected(db, participant)
            await db.commit()
        except DomainError:
            await db.rollback()
            return

    # Tell the remaining participants this person dropped; the session carries on.
    await hub.broadcast(
        str(session_id),
        server_frame(ServerEvent.DISCONNECTED, str(session_id), {"candidate_label": label}),
    )
