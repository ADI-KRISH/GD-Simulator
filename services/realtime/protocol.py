"""WebSocket wire protocol for the control plane (FR-09, design §10).

These are the names the browser sees. They are deliberately separate from the
internal `services.common.events.Event` names: the event bus is an
infrastructure contract between services, this is a UI contract. A rename on one
side must not silently break the other.
"""

from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field

from services.common.timeutil import utcnow


class ServerEvent(StrEnum):
    """Server → client. The FR-09 list plus the snapshot/error frames a real
    client needs."""

    USER_JOINED = "USER_JOINED"
    USER_LEFT = "USER_LEFT"
    USER_READY = "USER_READY"
    SPEECH_STARTED = "SPEECH_STARTED"
    SPEECH_STOPPED = "SPEECH_STOPPED"
    TURN_RECORDED = "TURN_RECORDED"
    # Practice mode: persona audio is announced over JSON, then streamed as binary.
    PRACTICE_READY = "PRACTICE_READY"
    USER_TRANSCRIPT = "USER_TRANSCRIPT"
    PERSONA_AUDIO_START = "PERSONA_AUDIO_START"
    PERSONA_AUDIO_READY = "PERSONA_AUDIO_READY"
    PERSONA_AUDIO_END = "PERSONA_AUDIO_END"
    PERSONA_AUDIO_FALLBACK = "PERSONA_AUDIO_FALLBACK"
    STOP_AUDIO = "STOP_AUDIO"
    TIMER_STARTED = "TIMER_STARTED"
    TIMER_UPDATED = "TIMER_UPDATED"
    SESSION_STARTED = "SESSION_STARTED"
    SESSION_ENDED = "SESSION_ENDED"
    DISCONNECTED = "DISCONNECTED"
    RECONNECTED = "RECONNECTED"

    # Not in the FR-09 list, but a client that reconnects mid-session needs the
    # current picture in one frame rather than replaying history.
    STATE_SNAPSHOT = "STATE_SNAPSHOT"
    TOPIC_RELEASED = "TOPIC_RELEASED"
    TRANSCRIPT_APPENDED = "TRANSCRIPT_APPENDED"
    METRICS_UPDATED = "METRICS_UPDATED"
    EVALUATION_COMPLETED = "EVALUATION_COMPLETED"
    NOTIFICATION = "NOTIFICATION"
    ERROR = "ERROR"


class ClientEvent(StrEnum):
    """Client → server."""

    READY = "READY"
    NOT_READY = "NOT_READY"
    SPEECH_STARTED = "SPEECH_STARTED"
    SPEECH_STOPPED = "SPEECH_STOPPED"
    PING = "PING"


class Frame(BaseModel):
    """Every message in both directions uses this envelope."""

    type: str
    session_id: str
    payload: dict[str, Any] = Field(default_factory=dict)
    at: str = Field(default_factory=lambda: utcnow().isoformat())


def server_frame(
    event: ServerEvent, session_id: str, payload: dict[str, Any] | None = None
) -> dict:
    return Frame(type=str(event), session_id=session_id, payload=payload or {}).model_dump()


def error_frame(session_id: str, code: str, message: str) -> dict:
    return server_frame(ServerEvent.ERROR, session_id, {"code": code, "message": message})
