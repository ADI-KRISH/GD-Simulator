"""HTTP API for the tracing dashboard.

Purpose:     Read-only endpoints over the span table: summary, per-session traces,
             the call list, one call in full, and evaluator quality signals.
Access:      Hosts and admins only: spans hold prompts, and prompts hold what
             students said.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query

from services.auth.deps import SessionDep, require_role
from services.auth.models import Role
from services.common.errors import NotFoundError
from services.observability import service

router = APIRouter(
    prefix="/api/v1/observability",
    tags=["observability"],
    dependencies=[Depends(require_role(Role.HOST, Role.ADMIN))],
)

Hours = Annotated[int, Query(ge=1, le=24 * 90)]


@router.get("/summary")
async def summary(db: SessionDep, hours: Hours = 24, session_id: str | None = None) -> dict:
    """Totals, breakdowns, speech and reply latency, and the time series."""
    return await service.summary(db, hours, session_id)


@router.get("/traces")
async def traces(db: SessionDep, hours: Hours = 24) -> list[dict]:
    """One roll-up per session."""
    return await service.traces(db, hours)


@router.get("/spans")
async def spans(
    db: SessionDep,
    hours: Hours = 24,
    kind: str | None = None,
    purpose: str | None = None,
    status: str | None = None,
    agent: str | None = None,
    model: str | None = None,
    session_id: str | None = None,
    q: str | None = Query(None, max_length=100),
    limit: int = Query(100, ge=1, le=500),
    offset: int = Query(0, ge=0),
) -> list[dict]:
    """Filterable list of calls."""
    return await service.list_spans(
        db, hours=hours, kind=kind, purpose=purpose, status=status, agent=agent, model=model,
        session_id=session_id, q=q, limit=limit, offset=offset,
    )  # fmt: skip


@router.get("/spans/{span_id}")
async def span(db: SessionDep, span_id: uuid.UUID) -> dict:
    """One call with its full prompt, output and tool calls."""
    found = await service.get_span(db, span_id)
    if found is None:
        raise NotFoundError("span not found")
    return found


@router.get("/quality")
async def quality(db: SessionDep, hours: Hours = 24 * 7) -> dict:
    """Evaluator confidence, critic findings and human-review rate."""
    return await service.quality(db, hours)
