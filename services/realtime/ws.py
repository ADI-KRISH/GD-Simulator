"""WebSocket control plane (FR-08, FR-09, design §10).

Application and control events only. Audio and video never travel through here
— that is the LiveKit media plane's job. Keeping the two apart is what lets a
media failure and a control failure be handled independently.

A dropped WebSocket marks the participant DISCONNECTED and nothing more: the
session keeps running (design §9, FR-38 candidate disconnect).
"""

import logging
import uuid
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query, WebSocket, WebSocketDisconnect
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.auth import repository as auth_repo
from services.common.db import get_session_factory
from services.common.errors import DomainError
from services.common.security import decode_token
from services.realtime.hub import Connection, hub
from services.realtime.protocol import ClientEvent, ServerEvent, error_frame, server_frame
from services.session import repository as session_repo
from services.session import service as session_service
from services.session.state import ParticipantState, SessionState

logger = logging.getLogger(__name__)

router = APIRouter(tags=["realtime"])

SessionFactoryDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]

# Close codes. 4001-4999 is the application-private range.
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404


async def _snapshot(db: AsyncSession, gd, viewer_label: str) -> dict[str, Any]:
    """Everything a (re)connecting client needs in one frame."""
    lobby = await session_service.lobby_view(db, gd)
    timer = session_service.timer_view(gd)
    topic = session_service.topic_view(gd)
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
    if not token:
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="missing token")
        return

    try:
        # `subject` is already a UUID — `decode_token` parses and validates it.
        user_id = decode_token(token, "access").subject
    except (DomainError, ValueError):
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="invalid token")
        return

    async with factory() as db:
        user = await auth_repo.get_user_by_id(db, user_id)
        gd = await session_repo.get_session_by_id(db, session_id)
        if user is None or gd is None:
            await websocket.close(code=CLOSE_NOT_FOUND, reason="session not found")
            return
        participant = await session_repo.get_participant(db, session_id, user_id)
        left = (
            participant is not None and ParticipantState(participant.state) is ParticipantState.LEFT
        )
        # The host watches their own session without being a candidate in it.
        is_observer = participant is None or left
        if is_observer and gd.host_id != user_id:
            await websocket.close(code=CLOSE_FORBIDDEN, reason="not a participant")
            return
        label = session_service.OBSERVER_LABEL if is_observer else participant.candidate_label
        # Re-entering after a drop is a reconnect, not a fresh join (design §9).
        was_disconnected = (
            not is_observer and ParticipantState(participant.state) is ParticipantState.DISCONNECTED
        )
        if was_disconnected:
            await session_service.mark_reconnected(db, participant)
        snapshot = await _snapshot(db, gd, label)
        await db.commit()

    await websocket.accept()
    connection = Connection(
        websocket=websocket, session_id=str(session_id), user_id=str(user_id), candidate_label=label
    )
    await hub.connect(connection)

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
        while True:
            raw = await websocket.receive_json()
            event = str(raw.get("type", ""))

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

            if event in {ClientEvent.READY, ClientEvent.NOT_READY}:
                ready = event == ClientEvent.READY
                async with factory() as db:
                    gd = await session_repo.get_session_by_id(db, session_id)
                    try:
                        await session_service.set_ready(db, gd, user, ready)
                        await db.commit()
                    except DomainError as exc:
                        await db.rollback()
                        await hub.send(
                            connection, error_frame(str(session_id), exc.code, exc.message)
                        )
                        continue
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

            if event == ClientEvent.SPEECH_STARTED:
                async with factory() as db:
                    gd = await session_repo.get_session_by_id(db, session_id)
                    if SessionState(gd.state) is not SessionState.LIVE:
                        continue
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

            if event == ClientEvent.SPEECH_STOPPED:
                if speaking_since is None:
                    continue
                async with factory() as db:
                    gd = await session_repo.get_session_by_id(db, session_id)
                    end = max(session_service.elapsed_discussion_seconds(gd), speaking_since)
                    # Persisted immediately — the metrics engine computes
                    # speaking time from these rows, never from an estimate.
                    _, changed = await session_service.record_speech_tracked(
                        db, gd, label, speaking_since, end
                    )
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
                await broadcast_turns(str(session_id), changed, interrupts)
                speaking_since = None
                continue

            await hub.send(
                connection, error_frame(str(session_id), "unknown_event", f"unsupported: {event}")
            )

    except WebSocketDisconnect:
        pass
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning(
            "websocket loop failed",
            extra={"session_id": str(session_id), "label": label, "error": str(exc)},
        )
    finally:
        await hub.disconnect(connection)
        # Only mark DISCONNECTED if this was the participant's last socket —
        # a second tab closing must not report them as gone.
        if not is_observer and not hub.is_connected(str(session_id), str(user_id)):
            await _record_disconnect(factory, session_id, user_id, label)


async def broadcast_turns(session_id: str, turns, interrupts: dict[str, list[float]]) -> None:
    """Tell the room about every turn that changed, with the running penalty."""
    from services.evaluation.aggregator import interruption_penalty

    for turn in turns:
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
        participant = await session_repo.get_participant(db, session_id, user_id)
        if participant is None:
            return
        try:
            await session_service.mark_disconnected(db, participant)
            await db.commit()
        except DomainError:
            await db.rollback()
            return

    await hub.broadcast(
        str(session_id),
        server_frame(ServerEvent.DISCONNECTED, str(session_id), {"candidate_label": label}),
    )
