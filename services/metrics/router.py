"""Metrics HTTP API (FR-18, design §17, §30)."""

import uuid

from fastapi import APIRouter
from pydantic import BaseModel

from services.auth.deps import CurrentUser, SessionDep
from services.common.contracts import CandidateMetrics, RollingState
from services.metrics import service
from services.observer import service as observer_service
from services.session import service as session_service

router = APIRouter(prefix="/api/v1/sessions", tags=["metrics"])


class InteractionEdgeOut(BaseModel):
    source: str
    target: str
    count: int


class MetricsOut(BaseModel):
    """Live and post-session metrics. Every number here was computed in code."""

    session_id: uuid.UUID
    total_speaking_seconds: float
    candidates: dict[str, CandidateMetrics]
    interactions: list[InteractionEdgeOut]
    dominant: list[str]
    under_participating: list[str]


@router.get("/{session_id}/metrics", response_model=MetricsOut)
async def get_metrics(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> MetricsOut:
    gd = await session_service.require_session(db, session_id)
    if gd.host_id != user.id:
        await session_service.require_participant(db, session_id, user)

    metrics = await service.build_metrics(db, session_id)
    return MetricsOut(
        session_id=session_id,
        total_speaking_seconds=metrics.total_speaking_seconds,
        candidates=metrics.per_candidate,
        interactions=[
            InteractionEdgeOut(source=e.source, target=e.target, count=e.count)
            for e in metrics.interactions
        ],
        dominant=metrics.dominant(),
        under_participating=metrics.under_participating(),
    )


@router.get("/{session_id}/observer", response_model=RollingState)
async def get_observer_state(
    session_id: uuid.UUID, db: SessionDep, user: CurrentUser
) -> RollingState:
    """The Observer's rolling understanding of the discussion so far.

    Observations only — the contract has no score field (claude.md invariant 7).
    """
    gd = await session_service.require_session(db, session_id)
    if gd.host_id != user.id:
        await session_service.require_participant(db, session_id, user)
    return await observer_service.current_state(db, session_id)
