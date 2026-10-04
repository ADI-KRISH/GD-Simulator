"""Domain event bus on Redis Streams (design §36, §37).

Producers publish; consumers read independently. Services must emit events here
rather than calling each other directly — that is what keeps the live plane and
the AI plane decoupled (claude.md invariant 1).

Redis Streams is the MVP transport; Kafka is the scale target (design §54). The
`publish` / `consume` surface is deliberately small so swapping it is a
single-file change.
"""

import json
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

from redis.asyncio import Redis

from services.common.redis_client import get_redis
from services.common.timeutil import utcnow

logger = logging.getLogger(__name__)

# One stream per domain keeps consumer groups small and lets the evaluation
# workers read only what they care about.
STREAM_SESSION = "gd:events:session"
STREAM_SPEECH = "gd:events:speech"
STREAM_EVALUATION = "gd:events:evaluation"

# Trim streams so a long-running local stack cannot grow without bound.
MAX_STREAM_LEN = 10_000


class Event(StrEnum):
    """The design §36 event list. Names are wire format — do not rename."""

    PARTICIPANT_JOINED = "participant.joined"
    PARTICIPANT_LEFT = "participant.left"
    PARTICIPANT_READY = "participant.ready"
    PARTICIPANT_DISCONNECTED = "participant.disconnected"
    PARTICIPANT_RECONNECTED = "participant.reconnected"
    SPEECH_STARTED = "speech.started"
    SPEECH_ENDED = "speech.ended"
    TRANSCRIPT_CREATED = "transcript.created"
    METRICS_UPDATED = "metrics.updated"
    INTERACTION_DETECTED = "interaction.detected"
    STATE_UPDATED = "state.updated"
    SESSION_STARTED = "session.started"
    SESSION_ENDED = "session.ended"
    EVALUATION_REQUESTED = "evaluation.requested"
    EVALUATION_COMPLETED = "evaluation.completed"
    REPORT_GENERATED = "report.generated"


_STREAM_FOR = {
    Event.SPEECH_STARTED: STREAM_SPEECH,
    Event.SPEECH_ENDED: STREAM_SPEECH,
    Event.TRANSCRIPT_CREATED: STREAM_SPEECH,
    Event.METRICS_UPDATED: STREAM_SPEECH,
    Event.INTERACTION_DETECTED: STREAM_SPEECH,
    Event.STATE_UPDATED: STREAM_SPEECH,
    Event.EVALUATION_REQUESTED: STREAM_EVALUATION,
    Event.EVALUATION_COMPLETED: STREAM_EVALUATION,
    Event.REPORT_GENERATED: STREAM_EVALUATION,
}


def stream_for(event: Event) -> str:
    return _STREAM_FOR.get(event, STREAM_SESSION)


@dataclass(slots=True)
class DomainEvent:
    event: Event
    session_id: str
    payload: dict[str, Any]
    occurred_at: str
    message_id: str = ""

    def to_wire(self) -> dict[str, str]:
        return {
            "event": str(self.event),
            "session_id": self.session_id,
            "occurred_at": self.occurred_at,
            "payload": json.dumps(self.payload, default=str),
        }

    @classmethod
    def from_wire(cls, message_id: str, fields: dict[str, str]) -> "DomainEvent":
        return cls(
            event=Event(fields["event"]),
            session_id=fields.get("session_id", ""),
            payload=json.loads(fields.get("payload") or "{}"),
            occurred_at=fields.get("occurred_at", ""),
            message_id=message_id,
        )


async def publish(
    event: Event,
    session_id: str,
    payload: dict[str, Any] | None = None,
    *,
    redis: Redis | None = None,
) -> str | None:
    """Emit a domain event. Never raises.

    A dead event bus must not take down a live GD (claude.md invariant 1), so a
    publish failure is logged and swallowed. Durable truth is already in
    Postgres by the time anything is published.
    """
    record = DomainEvent(
        event=event,
        session_id=session_id,
        payload=payload or {},
        occurred_at=utcnow().isoformat(),
    )
    try:
        client = redis or get_redis()
        return await client.xadd(
            stream_for(event),
            record.to_wire(),
            maxlen=MAX_STREAM_LEN,
            approximate=True,
        )
    except Exception as exc:  # pragma: no cover - depends on a live Redis
        logger.warning(
            "event publish failed",
            extra={"event": str(event), "session_id": session_id, "error": str(exc)},
        )
        return None


async def ensure_group(stream: str, group: str, *, redis: Redis | None = None) -> None:
    """Create a consumer group, tolerating one that already exists."""
    client = redis or get_redis()
    try:
        await client.xgroup_create(stream, group, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def consume(
    stream: str,
    group: str,
    consumer: str,
    *,
    block_ms: int = 5_000,
    count: int = 10,
    redis: Redis | None = None,
) -> AsyncIterator[DomainEvent]:
    """Read events for a consumer group. Caller acks with `ack`."""
    client = redis or get_redis()
    await ensure_group(stream, group, redis=client)
    while True:
        response = await client.xreadgroup(
            group, consumer, {stream: ">"}, count=count, block=block_ms
        )
        if not response:
            continue
        for _stream_name, messages in response:
            for message_id, fields in messages:
                yield DomainEvent.from_wire(message_id, fields)


async def ack(stream: str, group: str, message_id: str, *, redis: Redis | None = None) -> None:
    client = redis or get_redis()
    await client.xack(stream, group, message_id)
