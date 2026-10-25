"""Durable evaluation queue on Redis Streams (FR-19, design §19, §38, §39).

Purpose:     A thin wrapper over Redis Streams that carries evaluation jobs from
             "session ended" to the background worker, with retries and a
             dead-letter stream for jobs that keep failing.
Why needed:  Evaluation must be asynchronous and survive crashes (claude.md
             invariant 1): a worker that dies mid-job leaves the message pending so
             another worker can claim it, and a poisoned job stops after
             `MAX_ATTEMPTS` instead of looping forever.
Contains:
  - `QUEUE_STREAM`, `DEAD_LETTER_STREAM`, `CONSUMER_GROUP`, `MAX_ATTEMPTS`,
    `CLAIM_IDLE_MS`   the stream names and retry / takeover policy.
  - `QueuedJob`       a job read from the stream plus what's needed to ack it.
  - `ensure_group`    create the worker consumer group if missing.
  - `enqueue`         publish a job (never raises; the DB row is the safety net).
  - `_decode`         rebuild a `QueuedJob` from raw stream fields.
  - `read`            claim new jobs for a worker (the count is backpressure).
  - `claim_stalled`   take over jobs abandoned by a dead worker.
  - `ack`             mark a job processed so it isn't redelivered.
  - `retry`           requeue a failed job, or dead-letter it after the last try.
  - `depth`, `dead_letters`  observability: queue size and failed-job inspection.
Used by:     evaluation/service.py (enqueue), evaluation/worker.py (consume),
             evaluation/router.py (queue status endpoints), tests/test_evaluation_api.py.
Depends on:  common/contracts.py (`EvaluationJob`), common/events.py (announce
             `evaluation.requested`), common/redis_client.py.

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

import json  # (de)serialise the job payload in stream fields
import logging  # queue lifecycle logs
from dataclasses import dataclass  # QueuedJob container
from typing import Any  # raw stream fields

from redis.asyncio import Redis  # client type for the optional redis= argument

from services.common.contracts import EvaluationJob  # the job contract
from services.common.events import Event, publish  # announce evaluation.requested
from services.common.redis_client import get_redis  # shared client

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
    """A job as read from the stream, with the id needed to acknowledge it."""

    message_id: str
    job: EvaluationJob
    attempts: int

    @property
    def idempotency_key(self) -> str:
        """The wrapped job's idempotency key (session id + evaluation version)."""
        return self.job.idempotency_key


async def ensure_group(redis: Redis | None = None) -> None:
    """Create the worker consumer group on the queue stream if it does not exist."""
    client = redis or get_redis()
    try:
        await client.xgroup_create(QUEUE_STREAM, CONSUMER_GROUP, id="0", mkstream=True)
    except Exception as exc:
        # "BUSYGROUP" just means the group already exists, which is fine.
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
    # The job is serialised to JSON; "attempts" counts retries for the dead-letter rule.
    payload = {"job": job.model_dump_json(), "attempts": "0"}
    try:
        await ensure_group(client)
        message_id = await client.xadd(QUEUE_STREAM, payload)
    except Exception as exc:
        # Queue outage: log and carry on, so ending a session never fails because of it.
        logger.warning(
            "evaluation enqueue failed",
            extra={"session_id": job.session_id, "error": str(exc)},
        )
        return None

    # Announce the request on the event bus for anything else that cares.
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
    """Rebuild a QueuedJob from raw stream fields."""
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
    # ">" asks for messages never delivered to any consumer; blocks up to block_ms.
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
        # Ask Redis to reassign messages that have sat unacknowledged too long.
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
            # Unreadable stalled message: ack it so it can't be claimed forever.
            await client.xack(QUEUE_STREAM, CONSUMER_GROUP, message_id)
    return jobs


async def ack(message_id: str, *, redis: Redis | None = None) -> None:
    """Acknowledge a processed job so the group does not redeliver it."""
    client = redis or get_redis()
    await client.xack(QUEUE_STREAM, CONSUMER_GROUP, message_id)


async def retry(queued: QueuedJob, error: str, *, redis: Redis | None = None) -> bool:
    """Requeue a failed job, or dead-letter it once attempts run out.

    Returns True if it was requeued, False if it was dead-lettered.
    """
    client = redis or get_redis()
    attempts = queued.attempts + 1
    # Ack the failed delivery first; a fresh message (below) carries the retry.
    await ack(queued.message_id, redis=client)

    # Out of attempts: park the job in the dead-letter stream for inspection.
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

    # Otherwise put it back on the main stream with the bumped attempt count.
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
        # "pending" = delivered to a worker but not yet acknowledged.
        pending = await client.xpending(QUEUE_STREAM, CONSUMER_GROUP)
        return {
            "length": await client.xlen(QUEUE_STREAM),
            "pending": int(pending.get("pending", 0)) if isinstance(pending, dict) else 0,
            "dead_letter": await client.xlen(DEAD_LETTER_STREAM),
        }
    except Exception:
        # Monitoring must never crash on a Redis hiccup; report an empty queue.
        return {"length": 0, "pending": 0, "dead_letter": 0}


async def dead_letters(limit: int = 50, *, redis: Redis | None = None) -> list[dict]:
    """Inspect dead-lettered jobs so a poisoned one can be diagnosed."""
    client = redis or get_redis()
    try:
        entries = await client.xrange(DEAD_LETTER_STREAM, count=limit)
    except Exception:
        return []
    # Decode each entry's stored job JSON so the failure can be diagnosed.
    return [
        {
            "message_id": message_id,
            "attempts": int(fields.get("attempts", 0)),
            "error": fields.get("error", ""),
            "job": json.loads(fields.get("job", "{}")),
        }
        for message_id, fields in entries
    ]
