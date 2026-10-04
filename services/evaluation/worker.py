"""Evaluation worker (FR-19, design §19, §38).

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

import asyncio
import logging
import socket
import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.common.db import SessionFactory
from services.common.timeutil import as_utc, utcnow
from services.evaluation import queue, service
from services.evaluation import repository as repo
from services.evaluation.models import EvaluationJob
from services.session import repository as session_repo
from services.session.state import EvaluationState, SessionState

logger = logging.getLogger(__name__)

WORKER_INTERVAL_SECONDS = 5.0
# How long a job may sit QUEUED before the sweep assumes its queue message was
# lost and republishes it.
STALE_JOB_SECONDS = 120.0

CONSUMER_NAME = f"{socket.gethostname()}-{uuid.uuid4().hex[:8]}"


async def sweep(session: AsyncSession) -> int:
    """Create jobs for ended sessions and re-enqueue stale ones."""
    created = 0

    for gd in await session_repo.list_sessions_in_state(session, SessionState.ENDED):
        await service.request_evaluation(session, gd)
        created += 1

    cutoff = utcnow().timestamp() - STALE_JOB_SECONDS
    result = await session.execute(
        select(EvaluationJob).where(EvaluationJob.state == str(EvaluationState.QUEUED))
    )
    for job in result.scalars():
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

    try:
        await service.run_job(session, job)
        await session.commit()
        return True
    except Exception as exc:
        await session.rollback()
        async with SessionFactory() as fresh:
            failed = await repo.get_job(fresh, job.id)
            if failed is not None:
                await service.fail_job(fresh, failed, str(exc))
                await fresh.commit()
        logger.exception("evaluation job raised", extra={"job_id": str(job.id)})
        return False


async def drain(factory: async_sessionmaker[AsyncSession], *, block_ms: int = 1000) -> int:
    """Process whatever is currently on the queue. Returns jobs completed."""
    try:
        jobs = await queue.read(CONSUMER_NAME, count=1, block_ms=block_ms)
        jobs.extend(await queue.claim_stalled(CONSUMER_NAME))
    except Exception as exc:
        logger.debug("evaluation queue unavailable", extra={"error": str(exc)})
        return 0

    completed = 0
    for queued in jobs:
        async with factory() as session:
            ok = await _run_queued(session, queued)
        if ok:
            await queue.ack(queued.message_id)
            completed += 1
        else:
            await queue.retry(queued, "worker run failed")
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
            async with factory() as session:
                await sweep(session)
                await session.commit()
            await drain(factory)
        except asyncio.CancelledError:
            logger.info("evaluation worker stopped")
            raise
        except Exception as exc:
            logger.warning("evaluation worker tick failed", extra={"error": str(exc)})
        await asyncio.sleep(interval)
