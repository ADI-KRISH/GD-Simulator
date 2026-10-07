"""Solo practice HTTP + audio WebSocket.

`POST /api/v1/practice` opens a one-student session with AI personas.
`/ws/practice/{id}` is the live channel: the browser sends speech (JSON markers
plus one binary audio blob per utterance) and receives persona audio (JSON
markers plus binary mp3 chunks), captions, timer and turn events - all over one
socket, because the room's own hub fan-out already carries the clock and the
tracked turns.
"""

import json
import logging
import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, WebSocket, WebSocketDisconnect, status
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.auth import repository as auth_repo
from services.auth.deps import CurrentUser, SessionDep
from services.common.db import get_session_factory
from services.common.errors import DomainError
from services.common.security import decode_token
from services.practice.engine import STUDENT_LABEL, PracticeRoom
from services.practice.personas import PRESETS, resolve_personas
from services.realtime.hub import Connection, hub
from services.realtime.protocol import ServerEvent, error_frame, server_frame
from services.session import repository as session_repo
from services.session import service as session_service
from services.session.models import Difficulty, GDType
from services.session.schemas import SessionCreate, SessionOut
from services.speech.tts import get_tts_provider

logger = logging.getLogger(__name__)

router = APIRouter(tags=["practice"])

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
    try:
        personas = resolve_personas([p.model_dump(exclude_none=True) for p in payload.personas])
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc
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
    if not token:
        await websocket.close(code=CLOSE_UNAUTHORIZED, reason="missing token")
        return
    try:
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
        if gd.mode != "solo" or gd.host_id != user_id:
            await websocket.close(code=CLOSE_FORBIDDEN, reason="not your practice session")
            return
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

    await websocket.accept()
    connection = Connection(
        websocket=websocket,
        session_id=str(session_id),
        user_id=str(user_id),
        candidate_label=STUDENT_LABEL,
    )
    await hub.connect(connection)

    async def send_json(frame: dict) -> None:
        await websocket.send_json(frame)

    async def send_bytes(chunk: bytes) -> None:
        await websocket.send_bytes(chunk)

    room = PracticeRoom(factory, session_id, user.full_name, send_json, send_bytes)
    await room.load()
    await send_json(server_frame(ServerEvent.PRACTICE_READY, str(session_id), snapshot))
    # Reconnecting into a running session resumes the conversation loop.
    if snapshot["session_state"] in {"STARTING", "LIVE"}:
        await room.start_session(user)

    try:
        while True:
            message = await websocket.receive()
            if message.get("type") == "websocket.disconnect":
                break
            if message.get("bytes") is not None:
                await room.on_audio(message["bytes"])
                continue
            try:
                event = str(json.loads(message.get("text") or "{}").get("type", ""))
            except ValueError:
                continue
            if event == "START":
                try:
                    await room.start_session(user)
                except DomainError as exc:
                    await send_json(error_frame(str(session_id), exc.code, exc.message))
            elif event == "UTTERANCE_START":
                await room.on_utterance_start()
            elif event == "UTTERANCE_END":
                await room.on_utterance_end()
    except WebSocketDisconnect:
        pass
    except Exception:  # pragma: no cover - defensive
        logger.exception("practice socket failed", extra={"session_id": str(session_id)})
    finally:
        await room.close()
        await hub.disconnect(connection)
