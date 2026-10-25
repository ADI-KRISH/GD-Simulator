"""Domain event bus on Redis Streams (design §36, §37).

Producers publish; consumers read independently. Services must emit events here
rather than calling each other directly — that is what keeps the live plane and
the AI plane decoupled (claude.md invariant 1).

Redis Streams is the MVP transport; Kafka is the scale target (design §54). The
`publish` / `consume` surface is deliberately small so swapping it is a
single-file change.

Purpose:    define the event names and send/receive them over Redis Streams.
Why needed: the live discussion must never wait on, or fail because of, the AI
            side. Services announce "something happened" here and downstream
            consumers (evaluation) pick it up on their own time.
Contains:
    Event / STREAM_*   - the fixed event names (wire format) and the three streams.
    stream_for         - which stream an event goes to.
    DomainEvent        - one event as sent/received, with to_wire/from_wire.
    publish            - emit an event; never raises, so a dead bus cannot hurt a
                         live GD.
    ensure_group, consume, ack - consumer-group reading with explicit acking.
Used by:    evaluation/queue and service, metrics/service, observer/service,
            session/service, speech/service.
Depends on: common/logging, common/redis_client (shared client), common/timeutil
            (event timestamps).
"""

import json  # events travel as JSON payloads inside stream fields
import logging  # warn when a publish fails (it never raises)
from collections.abc import AsyncIterator  # consume() is an async generator
from dataclasses import dataclass  # DomainEvent container
from enum import StrEnum  # string-valued event names
from typing import Any  # free-form event payloads

from redis.asyncio import Redis  # client type for the optional redis= argument

from services.common.redis_client import get_redis  # shared client (or fail-fast stub)
from services.common.timeutil import utcnow  # timezone-aware "now" for occurred_at

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


# Event -> stream routing; anything not listed goes to the session stream.
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
    """Return the Redis stream an event is published to."""
    return _STREAM_FOR.get(event, STREAM_SESSION)


@dataclass(slots=True)
class DomainEvent:
    """One event as published and consumed: what happened, where, and when."""

    event: Event
    session_id: str
    payload: dict[str, Any]
    occurred_at: str
    message_id: str = ""  # Redis stream id, filled in when consumed

    def to_wire(self) -> dict[str, str]:
        """Flatten to the string-only fields a Redis stream entry accepts."""
        # Redis stream fields must be strings, so the payload becomes a JSON string.
        return {
            "event": str(self.event),
            "session_id": self.session_id,
            "occurred_at": self.occurred_at,
            "payload": json.dumps(self.payload, default=str),
        }

    @classmethod
    def from_wire(cls, message_id: str, fields: dict[str, str]) -> "DomainEvent":
        """Rebuild an event from a stream entry's fields."""
        # Reverse of to_wire; missing optional fields get safe defaults.
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
    # Stamp the event with its time at the moment it is created.
    record = DomainEvent(
        event=event,
        session_id=session_id,
        payload=payload or {},
        occurred_at=utcnow().isoformat(),
    )
    try:
        # Append to the right stream, capped so it cannot grow forever.
        client = redis or get_redis()
        return await client.xadd(
            stream_for(event),
            record.to_wire(),
            maxlen=MAX_STREAM_LEN,
            approximate=True,
        )
    except Exception as exc:  # pragma: no cover - depends on a live Redis
        # Swallow and log: losing an event is better than breaking a live session.
        logger.warning(
            "event publish failed",
            extra={"event": str(event), "session_id": session_id, "error": str(exc)},
        )
        return None


async def ensure_group(stream: str, group: str, *, redis: Redis | None = None) -> None:
    """Create a consumer group, tolerating one that already exists."""
    client = redis or get_redis()
    try:
        # mkstream=True also creates the stream if no event has arrived yet.
        await client.xgroup_create(stream, group, id="0", mkstream=True)
    except Exception as exc:
        # BUSYGROUP just means the group is already there; anything else is real.
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
    # Long-running loop: wait for new messages, yield them one by one.
    while True:
        response = await client.xreadgroup(
            group, consumer, {stream: ">"}, count=count, block=block_ms
        )
        # Nothing arrived within the block window; poll again.
        if not response:
            continue
        for _stream_name, messages in response:
            for message_id, fields in messages:
                yield DomainEvent.from_wire(message_id, fields)


async def ack(stream: str, group: str, message_id: str, *, redis: Redis | None = None) -> None:
    """Acknowledge a consumed message so the group does not redeliver it."""
    client = redis or get_redis()
    await client.xack(stream, group, message_id)
