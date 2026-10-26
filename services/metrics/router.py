"""Metrics HTTP API (FR-18, design §17, §30).

Purpose: HTTP endpoints that expose a session's computed metrics and the Observer's current
    rolling state.
Why needed: The frontend's live panels and the report page read measured numbers and the
    Observer's understanding through these routes, restricted to the host and participants.
Contains:
  - InteractionEdgeOut / MetricsOut: response shapes for the interaction graph and metrics.
  - get_metrics: freshly computed metrics for a session, with dominant and under-participating
    candidates.
  - get_observer_state: the Observer's latest rolling state (observations only, never scores).
Used by: services/main.py
Depends on: services/auth/deps.py, services/common/contracts.py, services/metrics/__init__.py,
    services/metrics/service.py, services/observer/__init__.py, services/session/__init__.py
"""

import uuid  # session id path parameter

from fastapi import APIRouter  # router
from pydantic import BaseModel  # response models

from services.auth.deps import CurrentUser, SessionDep  # injected user and DB session
from services.common.contracts import CandidateMetrics, RollingState  # response contracts
from services.metrics import service  # compute metrics
from services.observer import service as observer_service  # Observer's rolling state
from services.session import service as session_service  # access checks

router = APIRouter(prefix="/api/v1/sessions", tags=["metrics"])


class InteractionEdgeOut(BaseModel):
    """Who responded to whom, and how many times (an edge in the interaction graph)."""

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
    """Return computed metrics for a session (host or participants only)."""
    # 404 if the session does not exist.
    gd =await session_service.require_session(db, session_id)
    # Host or participants only.
    if gd.host_id != user.id:
        await session_service.require_participant(db, session_id, user)

    # Always computed fresh from the stored evidence.
    metrics = await service.build_metrics(db, session_id)
    # Convert engine objects into API models; the dominant and under-participating lists use the
    # thresholds defined in the engine.
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
    # The latest stored rolling state: observations, never scores.
    return await observer_service.current_state(db, session_id)
