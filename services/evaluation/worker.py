"""Evaluation worker (FR-19, design §19, §38).

Purpose:     The background loop that actually runs evaluation jobs: it pulls them
             off the Redis queue, executes them, acknowledges or retries them, and
             periodically sweeps the database for ended sessions that still need a
             job.
Why needed:  Evaluation is deliberately decoupled from the live session (claude.md
             invariant 1). This worker is the "downstream and asynchronous" half:
             nothing here can affect a live discussion, and a crashed job is
             picked up again rather than lost.
Contains:
  - `WORKER_INTERVAL_SECONDS`, `STALE_JOB_SECONDS`, `CONSUMER_NAME`  loop timing,
        the stale-job threshold and this worker's unique queue identity.
  - `sweep`                  create jobs for ended sessions; republish stale ones.
  - `_run_queued`            run one dequeued job (idempotent on redelivery).
  - `_run_job`               run a job, recording any failure on a fresh session.
  - `drain`                  process what is on the queue (falls back to the DB).
  - `drain_from_database`    no-Redis fallback: run QUEUED jobs straight from rows.
  - `run_worker`             the long-running loop started by the app.
Used by:     main.py (starts `run_worker` in the app lifespan) and
             tests/test_evaluation_api.py.
Depends on:  common/db.py, common/timeutil.py, evaluation/queue.py,
             evaluation/service.py, evaluation/repository.py,
             evaluation/models.py, session/repository.py and session/state.py.

Reads jobs off the durable queue and runs them. Runs in-process here because the
whole deployment is one machine (claude.md "Deployment — LOCAL ONLY"); nothing
about it assumes that, so moving it to its own process is a matter of running
`run_worker` from a separate entry point.

Alongside draining the queue it runs a **reconciliation sweep**. The job row is
written to Postgres and the queue message is published separately, which means
the two can disagree — a Redis restart between them would otherwise strand a
session in EVALUATING forever. The sweep re-enqueues anything the queue has
lost, which makes the durable row the authority and the queue merely the fast
path.
"""

import asyncio  # loop, sleep, cancellation
import logging  # worker lifecycle logs
import socket  # hostname for the consumer name
import uuid  # unique consumer suffix; id types

from sqlalchemy import select  # find queued/stuck jobs in the database
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # DB session types

from services.common.db import SessionFactory  # default session factory
from services.common.timeutil import as_utc, utcnow  # staleness checks
from services.evaluation import queue, service  # Redis queue and the job runner
from services.evaluation import repository as repo  # job data access
from services.evaluation.models import EvaluationJob  # job table row
from services.session import repository as session_repo  # session lookups
from services.session.state import EvaluationState, SessionState  # state filters

logger = logging.getLogger(__name__)

WORKER_INTERVAL_SECONDS = 5.0
# How long a job may sit QUEUED before the sweep assumes its queue message was
# lost and republishes it.
STALE_JOB_SECONDS = 120.0

CONSUMER_NAME = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


async def sweep(session: AsyncSession) -> int:
    """Create jobs for ended sessions and re-enqueue stale ones."""
    created = 0

    # Any session that has ENDED but has no evaluation yet gets a job.
    for gd in await session_repo.list_sessions_in_state(session, SessionState.ENDED):
        await service.request_evaluation(session, gd)
        created += 1

    # Find jobs stuck in QUEUED longer than the threshold: their queue message was
    # probably lost (e.g. Redis restarted), so publish them again.
    cutoff = utcnow().timestamp() - STALE_JOB_SECONDS
    result = await session.execute(
        select(EvaluationJob).where(EvaluationJob.state == str(EvaluationState.QUEUED))
    )
    for job in result.scalars():
        # Recently queued: the original message is probably still on its way.
        if as_utc(job.queued_at).timestamp() > cutoff:
            continue
        gd = await session_repo.get_session_by_id(session, job.session_id)
        if gd is None:
            continue
        participants = await session_repo.list_active_participants(session, gd.id)
        await queue.enqueue(
            service.to_contract(job, gd.topic, [p.candidate_label for p in participants])
        )
        logger.info("re-enqueued stale evaluation job", extra={"job_id": str(job.id)})

    return created


async def _run_queued(session: AsyncSession, queued: queue.QueuedJob) -> bool:
    """Run one dequeued job. Returns True on success."""
    # Look up the stored job by its idempotency key (session id + version).
    job = await repo.get_job_for_version(
        session, uuid.UUID(queued.job.session_id), queued.job.evaluation_version
    )
    if job is None:
        logger.warning(
            "queue referenced an unknown job",
            extra={"session_id": queued.job.session_id, "version": queued.job.evaluation_version},
        )
        return True  # Nothing to do; ack it rather than retrying forever.

    if EvaluationState(job.state) is EvaluationState.COMPLETED:
        # Redelivery of an already-finished job (design §39): ack and move on.
        return True

    return await _run_job(session, job)


async def _run_job(session: AsyncSession, job: EvaluationJob) -> bool:
    """Run one job, recording a failure on a fresh session. True on success."""
    # Read before any rollback: a rollback expires the row, and touching an expired
    # attribute afterwards triggers a lazy load that async SQLAlchemy cannot do.
    job_id = job.id
    try:
        # Do the evaluation and commit only if it fully succeeded.
        await service.run_job(session, job)
        await session.commit()
        return True
    except Exception as exc:
        # Undo the partial work, then record the failure on a clean session so
        # the failed state itself is not rolled back with it.
        await session.rollback()
        async with SessionFactory() as fresh:
            failed = await repo.get_job(fresh, job_id)
            if failed is not None:
                await service.fail_job(fresh, failed, str(exc))
                await fresh.commit()
        logger.exception("evaluation job raised", extra={"job_id": str(job_id)})
        return False


async def drain(factory: async_sessionmaker[AsyncSession], *, block_ms: int = 1000) -> int:
    """Process whatever is currently on the queue. Returns jobs completed."""
    try:
        # New messages first, then any abandoned by a worker that died.
        jobs = await queue.read(CONSUMER_NAME, count=1, block_ms=block_ms)
        jobs.extend(await queue.claim_stalled(CONSUMER_NAME))
    except Exception as exc:
        # Redis is down: degrade to running straight from the database rows.
        logger.debug("evaluation queue unavailable", extra={"error": str(exc)})
        return await drain_from_database(factory)

    completed = 0
    for queued in jobs:
        async with factory() as session:
            ok = await _run_queued(session, queued)
        # Success: acknowledge so it isn't redelivered. Failure: retry or dead-letter.
        if ok:
            await queue.ack(queued.message_id)
            completed += 1
        else:
            await queue.retry(queued, "worker run failed")
    return completed


async def drain_from_database(factory: async_sessionmaker[AsyncSession]) -> int:
    """Fallback when Redis is unreachable: run QUEUED jobs straight from the DB.

    The job row is the authority and the queue only the fast path (see the
    module docstring), so a missing queue degrades latency, never correctness.
    This is what lets the platform run with no Redis at all.

    ponytail: one worker, no claim/lock. Two workers without Redis could both
    pick the same job; the (session_id, evaluation_version) constraint keeps
    the result single, but it would be computed twice.
    """
    # Snapshot the ids of every QUEUED job first, then process each in its own session.
    async with factory() as session:
        result = await session.execute(
            select(EvaluationJob.id).where(EvaluationJob.state == str(EvaluationState.QUEUED))
        )
        job_ids = list(result.scalars())

    completed = 0
    for job_id in job_ids:
        async with factory() as session:
            job = await repo.get_job(session, job_id)
            # Re-check the state: another worker may have taken it since the snapshot.
            if job is None or EvaluationState(job.state) is not EvaluationState.QUEUED:
                continue
            if await _run_job(session, job):
                completed += 1
    return completed


async def run_worker(
    factory: async_sessionmaker[AsyncSession] | None = None,
    *,
    interval: float = WORKER_INTERVAL_SECONDS,
) -> None:
    """Long-running worker loop, started by the app lifespan."""
    factory = factory or SessionFactory
    logger.info("evaluation worker started", extra={"consumer": CONSUMER_NAME})
    while True:
        try:
            # Each tick: create/republish missing jobs, then process the queue.
            async with factory() as session:
                await sweep(session)
                await session.commit()
            await drain(factory)
        except asyncio.CancelledError:
            # Normal shutdown: let cancellation propagate so the app can stop cleanly.
            logger.info("evaluation worker stopped")
            raise
        except Exception as exc:
            # A failed tick degrades evaluation only; the loop keeps running.
            logger.warning("evaluation worker tick failed", extra={"error": str(exc)})
        await asyncio.sleep(interval)
