"""Solo practice HTTP + audio WebSocket.

Purpose:     The web entry points for practice mode: list the AI personas, create
             a one-student practice session, and run the live WebSocket over which
             the student's speech goes in and the personas' voices come out.
Why needed:  The browser can only reach the practice engine through these
             endpoints. The socket handler also does the authentication and
             ownership checks (only the session's own student may connect), then
             routes each browser event to the `PracticeRoom`.
Contains:
  - `CLOSE_UNAUTHORIZED`, `CLOSE_FORBIDDEN`, `CLOSE_NOT_FOUND`  WebSocket close codes.
  - `PersonaChoice`, `PracticeCreate`  request bodies (preset or custom persona).
  - `list_personas`      GET  /api/v1/practice/personas.
  - `create_practice`    POST /api/v1/practice (creates a "solo" session).
  - `practice_socket`    WS   /ws/practice/{session_id} (the live channel).
Used by:     main.py (mounts the router on the FastAPI app).
Depends on:  auth/ (user, token lookup), common/db.py, common/errors.py,
             common/security.py (`decode_token`), practice/engine.py
             (`PracticeRoom`), practice/personas.py, realtime/hub.py and
             realtime/protocol.py, session/ (repository, service, models,
             schemas) and speech/tts.py (how personas are voiced).

`POST /api/v1/practice` opens a one-student session with AI personas.
`/ws/practice/{id}` is the live channel: the browser sends speech (JSON markers
plus one binary audio blob per utterance) and receives persona audio (JSON
markers plus binary mp3 chunks), captions, timer and turn events - all over one
socket, because the room's own hub fan-out already carries the clock and the
tracked turns.
"""

import json  # parse text frames from the browser
import logging  # log unexpected socket failures
import uuid  # session id path parameter
from typing import Annotated  # Query/Depends metadata

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field  # request models
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # DB session types

from services.auth import repository as auth_repo  # look up the connecting user
from services.auth.deps import CurrentUser, SessionDep  # injected user and DB session
from services.common.db import get_session_factory  # sessions for the long-lived socket
from services.common.errors import DomainError  # expected failures
from services.common.security import decode_token  # verify the access token from the URL
from services.practice.engine import STUDENT_LABEL, PracticeRoom  # the turn-taking loop
from services.practice.personas import PRESETS, resolve_personas  # AI participants
from services.realtime.hub import Connection, hub  # shared room registry
from services.realtime.protocol import ServerEvent, error_frame, server_frame  # event frames
from services.session import repository as session_repo  # load the session
from services.session import service as session_service  # create session, topic/timer views
from services.session.models import Difficulty, GDType  # request enums
from services.session.schemas import SessionCreate, SessionOut  # create body / response
from services.speech.tts import get_tts_provider  # tells the client how personas are voiced

logger = logging.getLogger(__name__)

router = APIRouter(tags=["practice"])

# Same private close codes as the group-session socket.
CLOSE_UNAUTHORIZED = 4401
CLOSE_FORBIDDEN = 4403
CLOSE_NOT_FOUND = 4404

SessionFactoryDep = Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)]


class PersonaChoice(BaseModel):
    """A preset by key, or a custom persona (name + description + optional voice)."""

    preset: str | None = None
    name: str | None = Field(default=None, max_length=60)
    persona: str | None = Field(default=None, max_length=1000)
    voice_id: str | None = Field(default=None, max_length=64)


class PracticeCreate(BaseModel):
    """Body of POST /practice: the topic, timing and which AI personas to include."""

    topic: str = Field(min_length=5, max_length=2000)
    gd_type: GDType = GDType.GENERAL
    difficulty: Difficulty = Difficulty.INTERMEDIATE
    duration_seconds: int = Field(default=300, ge=60, le=1800)
    preparation_seconds: int = Field(default=20, ge=0, le=300)
    personas: list[PersonaChoice] = Field(min_length=1, max_length=4)


@router.get("/api/v1/practice/personas")
async def list_personas(user: CurrentUser) -> dict:
    """Presets to pick from, and how the personas will be voiced."""
    return {
        "personas": [p.public() for p in PRESETS.values()],
        "voice": "elevenlabs" if get_tts_provider().produces_audio else "browser",
    }


@router.post("/api/v1/practice", response_model=SessionOut, status_code=status.HTTP_201_CREATED)
async def create_practice(payload: PracticeCreate, db: SessionDep, user: CurrentUser) -> SessionOut:
    """Open a one-student "solo" session with the chosen personas."""
    # Turn the requested presets / custom personas into stored rows (labels, stances).
    try:
        personas = resolve_personas([p.model_dump(exclude_none=True) for p in payload.personas])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
    # A practice session is an ordinary session with one human and mode="solo".
    gd = await session_service.create_session(
        db,
        user,
        SessionCreate(
            topic=payload.topic,
            gd_type=payload.gd_type,
            difficulty=payload.difficulty,
            min_participants=1,
            max_participants=1,
            duration_seconds=payload.duration_seconds,
            preparation_seconds=payload.preparation_seconds,
            video_required=False,
            mode="solo",
            personas=personas,
        ),
    )
    return SessionOut.model_validate(gd)


@router.websocket("/ws/practice/{session_id}")
async def practice_socket(
    websocket: WebSocket,
    session_id: uuid.UUID,
    factory: SessionFactoryDep,
    token: Annotated[str | None, Query()] = None,
) -> None:
    """Live practice channel: speech in, persona audio/captions/timer/turns out."""
    # Authenticate: browsers can't set headers on a WebSocket, so the token is in the URL.
    if not token:
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="missing token")
        return
    try:
        user_id = decode_token(token, "access").subject
    except (DomainError, ValueError):
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="invalid token")
        return

    # Authorise: the session must exist, be a practice session, and belong to this user.
    async with factory() as db:
        user = await auth_repo.get_user_by_id(db, user_id)
        gd = await session_repo.get_session_by_id(db, session_id)
        if user is None or gd is None:
            await websocket.close(code=CLOSE_NOT_FOUND, reason="session not found")
            return
        if gd.mode != "solo" or gd.host_id != user_id:
            await websocket.close(code=CLOSE_FORBIDDEN, reason="not your practice session")
            return
        # The initial state the browser needs to draw the room (persona list, timer, voice mode).
        snapshot = {
            "you": STUDENT_LABEL,
            "session_state": str(gd.state),
            "topic": session_service.topic_view(gd).model_dump(mode="json"),
            "timer": session_service.timer_view(gd).model_dump(mode="json"),
            "personas": [
                {"label": p["label"], "name": p["name"], "eagerness": p.get("eagerness", 1.0)}
                for p in gd.personas
            ],
            "voice": "elevenlabs" if get_tts_provider().produces_audio else "browser",
            "duration_seconds": gd.duration_seconds,
        }

    # Checks passed: accept the socket and register it with the room hub so the
    # shared timer and turn events reach this browser.
    await websocket.accept()
    connection = Connection(
        websocket=websocket,
        session_id=str(session_id),
        user_id=str(user_id),
        candidate_label=STUDENT_LABEL,
    )
    await hub.connect(connection)

    async def send_json(frame: dict) -> None:
        """Callback the room uses to send a JSON event to this browser."""
        await websocket.send_json(frame)

    async def send_bytes(chunk: bytes) -> None:
        """Callback the room uses to stream binary persona audio to this browser."""
        await websocket.send_bytes(chunk)

    # The room runs the conversation; it talks to the browser only through these callbacks.
    room = PracticeRoom(factory, session_id, user.full_name, send_json, send_bytes)
    await room.load()
    await send_json(server_frame(ServerEvent.PRACTICE_READY, str(session_id), snapshot))
    # Reconnecting into a running session resumes the conversation loop.
    if snapshot["session_state"] in {"STARTING", "LIVE"}:
        await room.start_session(user)

    try:
        # Main loop: binary frames are speech audio; text frames are JSON control events.
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            # Binary frame = one utterance of the student's speech.
            if message.get("bytes") is not None:
                await room.on_audio(message["bytes"])
                continue
            # Text frame = a JSON control event; ignore anything malformed.
            try:
                data = json.loads(message.get("text") or "{}")
                event = str(data.get("type", "")) if isinstance(data, dict) else ""
            except ValueError:
                continue
            # Dispatch the event to the matching room action.
            if event == "TEXT":
                # FR-14.19: a typed turn. Queued, so a wait for a line in progress
                # never holds up this loop (which also carries PLAYBACK_ENDED).
                room.submit_text(str(data.get("text", "")))
            elif event == "RAISE_HAND":
                await room.on_raise_hand()
            elif event == "START":
                try:
                    await room.start_session(user)
                except DomainError as exc:
                    await send_json(error_frame(str(session_id), exc.code, exc.message))
            elif event == "UTTERANCE_START":
                await room.on_utterance_start()
            elif event == "UTTERANCE_END":
                await room.on_utterance_end()
            elif event == "PLAYBACK_ENDED":
                room.on_playback_ended()
    except WebSocketDisconnect:
        pass
    except Exception:  # pragma: no cover - defensive
        logger.exception("practice socket failed", extra={"session_id": str(session_id)})
    finally:
        # Always release the room and deregister the connection, however the socket ended.
        await room.close()
        await hub.disconnect(connection)
