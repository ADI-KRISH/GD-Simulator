"""Durable evaluation queue on Redis Streams (FR-19, design §19, §38, §39).

What the queue has to provide, per design §19: retry, backpressure, scheduling,
parallel workers and failure isolation. Redis Streams gives all five through
consumer groups — a message stays pending until acked, so a worker that dies
mid-job leaves the job claimable rather than lost.

Two things that matter more than the transport:

* **Idempotency is not the queue's job.** Redis Streams is at-least-once, so a
  job *will* occasionally be delivered twice. Correctness comes from the
  `(session_id, evaluation_version)` unique constraint in the database
  (design §39), not from hoping for exactly-once delivery.
* **A poisoned job stops.** After `MAX_ATTEMPTS` it goes to the dead-letter
  stream instead of cycling forever and starving healthy jobs.
"""

import json
import logging
from dataclasses import dataclass
from typing import Any

from redis.asyncio import Redis

from services.common.contracts import EvaluationJob
from services.common.events import Event, publish
from services.common.redis_client import get_redis

logger = logging.getLogger(__name__)

QUEUE_STREAM = "gd:queue:evaluation"
DEAD_LETTER_STREAM = "gd:queue:evaluation:dead"
CONSUMER_GROUP = "evaluation-workers"

# Design §38: the queue retries a failed worker. Three attempts is enough to
# ride out a transient LLM outage without hammering a genuinely broken job.
MAX_ATTEMPTS = 3

# A message pending longer than this is assumed to belong to a dead worker and
# may be claimed by another (design §38 evaluation worker failure).
CLAIM_IDLE_MS = 5 * 60 * 1000


@dataclass(slots=True)
class QueuedJob:
    message_id: str
    job: EvaluationJob
    attempts: int

    @property
    def idempotency_key(self) -> str:
        return self.job.idempotency_key


async def ensure_group(redis: Redis | None = None) -> None:
    client = redis or get_redis()
    try:
        await client.xgroup_create(QUEUE_STREAM, CONSUMER_GROUP, id="0", mkstream=True)
    except Exception as exc:
        if "BUSYGROUP" not in str(exc):
            raise


async def enqueue(job: EvaluationJob, *, redis: Redis | None = None) -> str | None:
    """Publish an evaluation job (design §20).

    Returns the stream message id, or None when Redis is unreachable. A failed
    enqueue is logged rather than raised: the job row is already durable in
    Postgres, so a worker sweep can pick it up even if this message was lost —
    and a queue outage must not fail the session-end path.
    """
    client = redis or get_redis()
    payload = {"job": job.model_dump_json(), "attempts": "0"}
    try:
        await ensure_group(client)
        message_id = await client.xadd(QUEUE_STREAM, payload)
    except Exception as exc:
        logger.warning(
            "evaluation enqueue failed",
            extra={"session_id": job.session_id, "error": str(exc)},
        )
        return None

    await publish(
        Event.EVALUATION_REQUESTED,
        job.session_id,
        {"evaluation_version": job.evaluation_version, "gd_type": job.gd_type},
        redis=client,
    )
    logger.info(
        "evaluation job enqueued",
        extra={"session_id": job.session_id, "message_id": message_id},
    )
    return message_id


def _decode(message_id: str, fields: dict[str, Any]) -> QueuedJob:
    return QueuedJob(
        message_id=message_id,
        job=EvaluationJob.model_validate_json(fields["job"]),
        attempts=int(fields.get("attempts", 0)),
    )


async def read(
    consumer: str, *, count: int = 1, block_ms: int = 5000, redis: Redis | None = None
) -> list[QueuedJob]:
    """Claim jobs for this worker. Backpressure is `count`."""
    client = redis or get_redis()
    await ensure_group(client)
    response = await client.xreadgroup(
        CONSUMER_GROUP, consumer, {QUEUE_STREAM: ">"}, count=count, block=block_ms
    )
    jobs: list[QueuedJob] = []
    for _stream, messages in response or []:
        for message_id, fields in messages:
            try:
                jobs.append(_decode(message_id, fields))
            except Exception as exc:
                # Undecodable message: nothing can ever process it, so it goes
                # straight to the dead letter stream rather than blocking.
                logger.error(
                    "undecodable queue message", extra={"message_id": message_id, "error": str(exc)}
                )
                await client.xadd(DEAD_LETTER_STREAM, {**fields, "error": str(exc)})
                await client.xack(QUEUE_STREAM, CONSUMER_GROUP, message_id)
    return jobs


async def claim_stalled(
    consumer: str, *, count: int = 10, redis: Redis | None = None
) -> list[QueuedJob]:
    """Take over messages abandoned by a worker that died mid-job."""
    client = redis or get_redis()
    await ensure_group(client)
    try:
        result = await client.xautoclaim(
            QUEUE_STREAM, CONSUMER_GROUP, consumer, min_idle_time=CLAIM_IDLE_MS, count=count
        )
    except Exception as exc:
        logger.warning("xautoclaim failed", extra={"error": str(exc)})
        return []

    # redis-py returns (next_cursor, messages) or (next_cursor, messages, deleted).
    messages = result[1] if len(result) >= 2 else []
    jobs: list[QueuedJob] = []
    for message_id, fields in messages:
        try:
            jobs.append(_decode(message_id, fields))
        except Exception:
            await client.xack(QUEUE_STREAM, CONSUMER_GROUP, message_id)
    return jobs


async def ack(message_id: str, *, redis: Redis | None = None) -> None:
    client = redis or get_redis()
    await client.xack(QUEUE_STREAM, CONSUMER_GROUP, message_id)


async def retry(queued: QueuedJob, error: str, *, redis: Redis | None = None) -> bool:
    """Requeue a failed job, or dead-letter it once attempts run out.

    Returns True if it was requeued, False if it was dead-lettered.
    """
    client = redis or get_redis()
    attempts = queued.attempts + 1
    await ack(queued.message_id, redis=client)

    if attempts >= MAX_ATTEMPTS:
        await client.xadd(
            DEAD_LETTER_STREAM,
            {"job": queued.job.model_dump_json(), "attempts": str(attempts), "error": error[:2000]},
        )
        logger.error(
            "evaluation job dead-lettered",
            extra={
                "session_id": queued.job.session_id,
                "attempts": attempts,
                "error": error,
            },
        )
        return False

    await client.xadd(
        QUEUE_STREAM, {"job": queued.job.model_dump_json(), "attempts": str(attempts)}
    )
    logger.warning(
        "evaluation job requeued",
        extra={"session_id": queued.job.session_id, "attempts": attempts, "error": error},
    )
    return True


async def depth(*, redis: Redis | None = None) -> dict[str, int]:
    """Queue depth and dead-letter size — design §41 observability."""
    client = redis or get_redis()
    try:
        pending = await client.xpending(QUEUE_STREAM, CONSUMER_GROUP)
        return {
            "length": await client.xlen(QUEUE_STREAM),
            "pending": int(pending.get("pending", 0)) if isinstance(pending, dict) else 0,
            "dead_letter": await client.xlen(DEAD_LETTER_STREAM),
        }
    except Exception:
        return {"length": 0, "pending": 0, "dead_letter": 0}


async def dead_letters(limit: int = 50, *, redis: Redis | None = None) -> list[dict]:
    """Inspect dead-lettered jobs so a poisoned one can be diagnosed."""
    client = redis or get_redis()
    try:
        entries = await client.xrange(DEAD_LETTER_STREAM, count=limit)
    except Exception:
        return []
    return [
        {
            "message_id": message_id,
            "attempts": int(fields.get("attempts", 0)),
            "error": fields.get("error", ""),
            "job": json.loads(fields.get("job", "{}")),
        }
        for message_id, fields in entries
    ]
