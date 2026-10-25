"""WebSocket wire protocol for the control plane (FR-09, design §10).

These are the names the browser sees. They are deliberately separate from the
internal `services.common.events.Event` names: the event bus is an
infrastructure contract between services, this is a UI contract. A rename on one
side must not silently break the other.

Purpose: The wire format of the WebSocket control plane: the server and client event names, the
    common Frame envelope and helpers to build frames.
Why needed: Browser and server must agree on event names and message shape; keeping them here,
    separate from the internal event bus names, means a UI rename cannot silently break backend
    services or the other way round.
Contains:
  - ServerEvent: every event the server can push to browsers (lobby, speech, timer, practice,
    snapshot, errors).
  - ClientEvent: the few events a browser may send (ready, not ready, speech start/stop, ping).
  - Frame: the envelope {type, session_id, payload, at} used in both directions.
  - server_frame: build a server-to-client message dict.
  - error_frame: build an ERROR frame with a machine code and a readable message.
Used by: services/evaluation/service.py, services/lobby/router.py,
    services/notifications/service.py, services/observer/service.py,
    services/practice/engine.py, services/practice/router.py, services/realtime/timer.py,
    services/realtime/ws.py, services/session/router.py, services/simulation.py,
    services/speech/router.py, services/tests/test_realtime_ws.py
Depends on: services/common/timeutil.py
"""

from enum import StrEnum  # string-valued event names (wire format)
from typing import Any  # free-form payloads

from pydantic import BaseModel, Field  # the Frame envelope model

from services.common.timeutil import utcnow  # timestamp each frame


class ServerEvent(StrEnum):
    """Server → client. The FR-09 list plus the snapshot/error frames a real
    client needs."""

    # Lobby membership changes, pushed by the lobby and websocket handlers.
    USER_JOINED = "USER_JOINED"
    USER_LEFT = "USER_LEFT"
    USER_READY = "USER_READY"
    # Live speech activity and the turns it produces.
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
    # A simulated participant's spoken line (mp3, base64) for the room to play.
    AGENT_AUDIO = "AGENT_AUDIO"
    # Clock and phase changes from the ticker (realtime/timer.py).
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
    # M14 FR-14.13: the latest ideas on the table, from the Observer's rolling state.
    IDEAS_UPDATED = "IDEAS_UPDATED"
    EVALUATION_COMPLETED = "EVALUATION_COMPLETED"
    NOTIFICATION = "NOTIFICATION"
    ERROR = "ERROR"


class ClientEvent(StrEnum):
    """Client → server."""

    # Lobby readiness and speech activity sent by the browser; PING keeps the socket alive.
    READY = "READY"
    NOT_READY = "NOT_READY"
    SPEECH_STARTED = "SPEECH_STARTED"
    SPEECH_STOPPED = "SPEECH_STOPPED"
    PING = "PING"


class Frame(BaseModel):
    """Every message in both directions uses this envelope."""

    # The event name (a ServerEvent or ClientEvent value).
    type: str
    session_id: str
    # Event-specific data; its shape depends on the event type.
    payload: dict[str, Any] = Field(default_factory=dict)
    # Server timestamp so clients can order and age frames.
    at: str = Field(default_factory=lambda: utcnow().isoformat())


def server_frame(
    event: ServerEvent, session_id: str, payload: dict[str, Any] | None = None
) -> dict:
    """Build a server → client message dict in the standard Frame envelope."""
    # Frame validates the envelope; model_dump gives a plain dict that send_json can serialise.
    return Frame(type=str(event), session_id=session_id, payload=payload or {}).model_dump()


def error_frame(session_id: str, code: str, message: str) -> dict:
    """Build an ERROR frame with a machine code and a human-readable message."""
    # Errors are ordinary frames, so the client handles them on the same code path.
    return server_frame(ServerEvent.ERROR, session_id, {"code": code, "message": message})
