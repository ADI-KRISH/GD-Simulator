"""Evaluation and report HTTP API (FR-20, FR-30 – FR-32, design §33, §34, §52)."""

import uuid
from datetime import datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, Field

from services.auth.deps import CurrentUser, SessionDep, require_role
from services.auth.models import Role
from services.common.errors import NotFoundError, PermissionDeniedError
from services.evaluation import queue, service
from services.evaluation import repository as repo
from services.evaluation.models import EvaluationJob as EvaluationJobRow
from services.session import service as session_service
from services.session.state import EvaluationState

router = APIRouter(prefix="/api/v1", tags=["evaluation"])

AdminOnly = Annotated[object, Depends(require_role(Role.ADMIN))]


class EvaluationStatus(BaseModel):
    # The job's primary key is `id` on the row; it is `job_id` on the wire so a
    # client holding several ids at once cannot confuse it with a session id.
    job_id: uuid.UUID
    session_id: uuid.UUID
    evaluation_version: int
    state: EvaluationState
    gd_type: str
    rubric: dict[str, float]
    rubric_version: str
    prompt_version: str
    agent_version: str
    selected_agents: list[str]
    attempts: int
    error: str | None
    queued_at: datetime
    completed_at: datetime | None


def _status(job: EvaluationJobRow) -> EvaluationStatus:
    """Row -> response. Written out rather than `model_validate(from_attributes=True)`
    because the primary key is renamed on the wire."""
    return EvaluationStatus(
        job_id=job.id,
        session_id=job.session_id,
        evaluation_version=job.evaluation_version,
        state=EvaluationState(job.state),
        gd_type=job.gd_type,
        rubric=job.rubric,
        rubric_version=job.rubric_version,
        prompt_version=job.prompt_version,
        agent_version=job.agent_version,
        selected_agents=job.selected_agents,
        attempts=job.attempts,
        error=job.error,
        queued_at=job.queued_at,
        completed_at=job.completed_at,
    )


class ScoreOut(BaseModel):
    candidate_label: str
    overall: float
    dimensions: dict[str, float]
    weights: dict[str, float]
    confidence: float
    needs_human_review: bool


class ReportOut(BaseModel):
    session_id: uuid.UUID
    candidate_label: str | None
    body: dict[str, Any]
    created_at: datetime


class HistoryEntry(BaseModel):
    """FR-31."""

    session_id: uuid.UUID
    topic: str
    overall: float
    dimensions: dict[str, float]
    created_at: datetime


class HistoryOut(BaseModel):
    sessions_completed: int
    average_overall: float | None
    trend: dict[str, float] = Field(default_factory=dict)
    entries: list[HistoryEntry]


@router.post("/sessions/{session_id}/evaluate", response_model=EvaluationStatus)
async def request_evaluation(
    session_id: uuid.UUID,
    db: SessionDep,
    user: CurrentUser,
    run_now: Annotated[bool, Query(description="Run inline instead of queueing")] = False,
) -> EvaluationStatus:
    """FR-20. Host-triggered evaluation.

    Normally the background worker picks the session up automatically when it
    ends; this exists for running without a worker and for re-running after a
    failure.

    Idempotent (design §39). The worker and this endpoint race by design — both
    are meant to be able to evaluate a session — so an already-completed job is
    returned as-is rather than re-run. Without that, whichever lost the race
    would fail on `COMPLETED → PREPARING`.
    """
    gd = await session_service.require_session(db, session_id)
    session_service.require_host(gd, user)
    job = await service.request_evaluation(db, gd)
    if run_now and EvaluationState(job.state) is not EvaluationState.COMPLETED:
        job = await service.run_job(db, job)
    return _status(job)


@router.get("/sessions/{session_id}/evaluation", response_model=EvaluationStatus)
async def get_evaluation_status(
    session_id: uuid.UUID, db: SessionDep, user: CurrentUser
) -> EvaluationStatus:
    gd = await session_service.require_session(db, session_id)
    if gd.host_id != user.id:
        await session_service.require_participant(db, session_id, user)
    job = await repo.latest_job(db, session_id)
    if job is None:
        raise NotFoundError("this session has not been evaluated")
    return _status(job)


@router.get("/sessions/{session_id}/scores", response_model=list[ScoreOut])
async def get_scores(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> list[ScoreOut]:
    """Every candidate's score. Host-only — a candidate sees their own report."""
    gd = await session_service.require_session(db, session_id)
    session_service.require_host(gd, user)
    job = await repo.latest_job(db, session_id)
    if job is None:
        raise NotFoundError("this session has not been evaluated")
    return [
        ScoreOut(
            candidate_label=s.candidate_label,
            overall=s.overall,
            dimensions=s.dimensions,
            weights=s.weights,
            confidence=s.confidence,
            needs_human_review=s.needs_human_review,
        )
        for s in await repo.list_scores(db, job.id)
    ]


@router.get("/sessions/{session_id}/report", response_model=ReportOut)
async def get_my_report(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> ReportOut:
    """FR-30. A candidate's own report (NFR-07: they see only theirs)."""
    participant = await session_service.require_participant(db, session_id, user)
    report = await repo.get_candidate_report(db, session_id, participant.candidate_label)
    if report is None:
        raise NotFoundError("no report yet for this session")
    return ReportOut(
        session_id=session_id,
        candidate_label=report.candidate_label,
        body=report.body,
        created_at=report.created_at,
    )


@router.get("/sessions/{session_id}/report/group", response_model=ReportOut)
async def get_group_report(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> ReportOut:
    """Design §34. Host-only: it names dominant and under-participating candidates."""
    gd = await session_service.require_session(db, session_id)
    session_service.require_host(gd, user)
    report = await repo.get_group_report(db, session_id)
    if report is None:
        raise NotFoundError("no group report yet for this session")
    return ReportOut(
        session_id=session_id,
        candidate_label=None,
        body=report.body,
        created_at=report.created_at,
    )


@router.get("/sessions/{session_id}/report/{candidate_label}", response_model=ReportOut)
async def get_candidate_report(
    session_id: uuid.UUID, candidate_label: str, db: SessionDep, user: CurrentUser
) -> ReportOut:
    """A specific candidate's report. Host, admin, or that candidate."""
    gd = await session_service.require_session(db, session_id)
    if gd.host_id != user.id and user.role != Role.ADMIN.value:
        participant = await session_service.require_participant(db, session_id, user)
        if participant.candidate_label != candidate_label:
            raise PermissionDeniedError("you can only read your own report")
    report = await repo.get_candidate_report(db, session_id, candidate_label)
    if report is None:
        raise NotFoundError("no report for that candidate")
    return ReportOut(
        session_id=session_id,
        candidate_label=report.candidate_label,
        body=report.body,
        created_at=report.created_at,
    )


@router.get("/me/history", response_model=HistoryOut)
async def get_my_history(db: SessionDep, user: CurrentUser) -> HistoryOut:
    """FR-31. Performance across every GD this candidate has completed."""
    scores = await repo.list_scores_for_user(db, user.id)
    entries: list[HistoryEntry] = []
    for score in scores:
        job = await repo.get_job(db, score.job_id)
        if job is None:
            continue
        gd = await session_service.require_session(db, job.session_id)
        entries.append(
            HistoryEntry(
                session_id=gd.id,
                topic=gd.topic,
                overall=score.overall,
                dimensions=score.dimensions,
                created_at=score.created_at,
            )
        )

    average = round(sum(e.overall for e in entries) / len(entries), 2) if entries else None
    # Per-dimension averages, so weakness detection has something to work with
    # (design §53 personalization inputs).
    trend: dict[str, float] = {}
    if entries:
        totals: dict[str, list[float]] = {}
        for entry in entries:
            for dimension, value in entry.dimensions.items():
                totals.setdefault(dimension, []).append(value)
        trend = {d: round(sum(v) / len(v), 3) for d, v in sorted(totals.items())}

    return HistoryOut(
        sessions_completed=len(entries), average_overall=average, trend=trend, entries=entries
    )


@router.get("/evaluation/queue", response_model=dict)
async def get_queue_depth(db: SessionDep, user: CurrentUser, _: AdminOnly) -> dict:
    """Design §41: queue depth and dead-letter size."""
    return {"queue": await queue.depth(), "dead_letters": await queue.dead_letters(limit=10)}
