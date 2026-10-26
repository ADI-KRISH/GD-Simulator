"""Data access for evaluation jobs, agent outputs, scores and reports.

Purpose:     Every database read/write for the evaluation tables lives here as
             small async functions (get/list/add/clear), so no other module writes
             raw queries against them.
Why needed:  Keeping SQL in one place makes the idempotency lookup, retry cleanup
             and history queries easy to find and test, and keeps the service and
             router layers free of query details.
Contains:
  - `get_job`, `get_job_for_version`, `latest_job`, `next_version`, `add_job`
        job lookup (including the idempotency check, design §39) and creation.
  - `add_agent_output`, `list_agent_outputs`, `clear_agent_outputs`
        store/read specialist verdicts; clearing wipes a partial run before a retry.
  - `add_score`, `list_scores`, `list_scores_for_user`
        aggregated scores, per job and per user (feeds the history page).
  - `add_report`, `list_reports_for_job`, `get_candidate_report`,
    `get_group_report`, `list_reports_for_user`  stored reports and history.
Used by:     evaluation/service.py, evaluation/worker.py, evaluation/router.py,
             tests/test_evaluation_pipeline.py.
Depends on:  evaluation/models.py (the four tables).
"""

import uuid  # id types

from sqlalchemy import select  # build queries
from sqlalchemy.ext.asyncio import AsyncSession  # database session

from services.evaluation.models import AgentOutput, CandidateScore, EvaluationJob, Report  # tables


async def get_job(session: AsyncSession, job_id: uuid.UUID) -> EvaluationJob | None:
    """Load an evaluation job by primary key."""
    return await session.get(EvaluationJob, job_id)


async def get_job_for_version(
    session: AsyncSession, session_id: uuid.UUID, version: int
) -> EvaluationJob | None:
    """The idempotency lookup (design §39)."""
    result = await session.execute(
        select(EvaluationJob).where(
            EvaluationJob.session_id == session_id,
            EvaluationJob.evaluation_version == version,
        )
    )
    return result.scalar_one_or_none()


async def latest_job(session: AsyncSession, session_id: uuid.UUID) -> EvaluationJob | None:
    """The session's most recent evaluation attempt (highest version), if any."""
    result = await session.execute(
        select(EvaluationJob)
        .where(EvaluationJob.session_id == session_id)
        .order_by(EvaluationJob.evaluation_version.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def next_version(session: AsyncSession, session_id: uuid.UUID) -> int:
    """The evaluation_version a new attempt for this session should use."""
    latest = await latest_job(session, session_id)
    return 1 if latest is None else latest.evaluation_version + 1


async def add_job(session: AsyncSession, job: EvaluationJob) -> EvaluationJob:
    """Insert a new evaluation job."""
    session.add(job)
    # Flush so the database assigns defaults, then refresh to load them onto the object.
    await session.flush()
    await session.refresh(job)
    return job


async def add_agent_output(session: AsyncSession, output: AgentOutput) -> AgentOutput:
    """Store one validated specialist-agent output."""
    session.add(output)
    await session.flush()
    return output


async def list_agent_outputs(session: AsyncSession, job_id: uuid.UUID) -> list[AgentOutput]:
    """All agent outputs produced for a job."""
    result = await session.execute(
        select(AgentOutput)
        .where(AgentOutput.job_id == job_id)
        .order_by(AgentOutput.candidate_label, AgentOutput.dimension)
    )
    return list(result.scalars())


async def clear_agent_outputs(session: AsyncSession, job_id: uuid.UUID) -> None:
    """Wipe a partial run before retrying it.

    Reprocessing must not leave a mixture of old and new agent verdicts behind
    the same job id — that would make the stored evidence disagree with the
    score derived from it (design §39).
    """
    # Delete verdicts, scores and reports together so the three stay consistent.
    for output in await list_agent_outputs(session, job_id):
        await session.delete(output)
    for score in await list_scores(session, job_id):
        await session.delete(score)
    for report in await list_reports_for_job(session, job_id):
        await session.delete(report)
    await session.flush()


async def add_score(session: AsyncSession, score: CandidateScore) -> CandidateScore:
    """Store one candidate's final computed score."""
    session.add(score)
    await session.flush()
    return score


async def list_scores(session: AsyncSession, job_id: uuid.UUID) -> list[CandidateScore]:
    """Every candidate's score for a job."""
    result = await session.execute(
        select(CandidateScore)
        .where(CandidateScore.job_id == job_id)
        .order_by(CandidateScore.candidate_label)
    )
    return list(result.scalars())


async def add_report(session: AsyncSession, report: Report) -> Report:
    """Store a candidate or group report."""
    session.add(report)
    await session.flush()
    return report


async def list_reports_for_job(session: AsyncSession, job_id: uuid.UUID) -> list[Report]:
    """All reports (candidate and group) produced by a job."""
    result = await session.execute(select(Report).where(Report.job_id == job_id))
    return list(result.scalars())


async def get_candidate_report(
    session: AsyncSession, session_id: uuid.UUID, candidate_label: str
) -> Report | None:
    """The latest report for one candidate in a session."""
    result = await session.execute(
        select(Report)
        .where(Report.session_id == session_id, Report.candidate_label == candidate_label)
        .order_by(Report.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def get_group_report(session: AsyncSession, session_id: uuid.UUID) -> Report | None:
    """The latest group-level report for a session."""
    result = await session.execute(
        select(Report)
        .where(Report.session_id == session_id, Report.candidate_label.is_(None))
        .order_by(Report.created_at.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_reports_for_user(
    session: AsyncSession, user_id: uuid.UUID, limit: int = 50
) -> list[Report]:
    """FR-31 historical analysis: every report this candidate has received."""
    result = await session.execute(
        select(Report)
        .where(Report.user_id == user_id, Report.candidate_label.is_not(None))
        .order_by(Report.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars())


async def list_scores_for_user(
    session: AsyncSession, user_id: uuid.UUID, limit: int = 100
) -> list[CandidateScore]:
    """A user's scores across sessions (feeds the history page); the newest `limit`."""
    result = await session.execute(
        select(CandidateScore)
        .where(CandidateScore.user_id == user_id)
        .order_by(CandidateScore.created_at.desc())
        .limit(limit)
    )
    return list(result.scalars())
