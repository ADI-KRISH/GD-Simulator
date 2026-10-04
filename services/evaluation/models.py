"""Evaluation records (FR-20 – FR-30, design §19, §39, §40).

Four tables, each answering a different question after the fact:

* `evaluation_jobs`  — what was asked for, under which configuration versions
* `agent_outputs`    — what each agent said, raw, before aggregation
* `candidate_scores` — what the deterministic aggregator computed from them
* `reports`          — what the candidate was shown

Keeping agent outputs separate from scores is what makes design §32 real: the
final number is always re-derivable from stored evidence plus the stored
weights, so "which configuration produced this score" has an answer.
"""

import uuid
from datetime import datetime

from sqlalchemy import (
    DateTime,
    Float,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    Uuid,
    func,
)
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from services.common.db import Base
from services.session.state import EvaluationState


class EvaluationJob(Base):
    """One evaluation attempt for one session (design §20).

    `(session_id, evaluation_version)` is unique — that *is* the idempotency key
    from design §39, enforced by the database rather than by convention, so a
    redelivered queue message cannot create a second report.
    """

    __tablename__ = "evaluation_jobs"
    __table_args__ = (
        UniqueConstraint("session_id", "evaluation_version", name="uq_evaluation_job_version"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("gd_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    evaluation_version: Mapped[int] = mapped_column(Integer, default=1, nullable=False)

    state: Mapped[str] = mapped_column(
        String(24), default=EvaluationState.QUEUED, index=True, nullable=False
    )
    gd_type: Mapped[str] = mapped_column(String(20), nullable=False)
    rubric: Mapped[dict] = mapped_column(JSON, nullable=False)

    # Design §40: the reproducibility triple, frozen at enqueue time.
    rubric_version: Mapped[str] = mapped_column(String(20), nullable=False)
    prompt_version: Mapped[str] = mapped_column(String(20), nullable=False)
    agent_version: Mapped[str] = mapped_column(String(20), nullable=False)

    selected_agents: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)

    queued_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AgentOutput(Base):
    """One evaluator agent's verdict on one candidate, one dimension (design §50).

    Stored exactly as the agent produced it, after contract validation and
    before aggregation. `confidence` is kept because design §51/§52 route
    low-confidence results to human review.
    """

    __tablename__ = "agent_outputs"
    __table_args__ = (
        UniqueConstraint(
            "job_id", "candidate_label", "dimension", name="uq_agent_output_dimension"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("evaluation_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    candidate_label: Mapped[str] = mapped_column(String(4), index=True, nullable=False)
    dimension: Mapped[str] = mapped_column(String(24), nullable=False)
    agent_name: Mapped[str] = mapped_column(String(40), nullable=False)

    score: Mapped[float] = mapped_column(Float, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    strengths: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    weaknesses: Mapped[list] = mapped_column(JSON, default=list, nullable=False)
    evidence: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class CandidateScore(Base):
    """Aggregated result for one candidate (design §31).

    `overall` is computed in code from `dimensions` and the job's stored weights.
    No LLM ever writes this row (claude.md invariant 5).
    """

    __tablename__ = "candidate_scores"
    __table_args__ = (UniqueConstraint("job_id", "candidate_label", name="uq_candidate_score_job"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("evaluation_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    candidate_label: Mapped[str] = mapped_column(String(4), nullable=False)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )

    # 0-100, the scale the report shows (design §33).
    overall: Mapped[float] = mapped_column(Float, nullable=False)
    # dimension -> 0-10 score, as the agents reported it.
    dimensions: Mapped[dict] = mapped_column(JSON, nullable=False)
    # The weights actually applied, copied here so the arithmetic is auditable
    # even if the session's rubric is later edited.
    weights: Mapped[dict] = mapped_column(JSON, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)
    needs_human_review: Mapped[bool] = mapped_column(default=False, nullable=False)

    metrics: Mapped[dict] = mapped_column(JSON, default=dict, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Report(Base):
    """Rendered report (FR-30, design §33, §34).

    One per candidate, plus one group-level row per job where
    `candidate_label` is null.
    """

    __tablename__ = "reports"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    job_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("evaluation_jobs.id", ondelete="CASCADE"), index=True, nullable=False
    )
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("gd_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    candidate_label: Mapped[str | None] = mapped_column(String(4), index=True, nullable=True)
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="SET NULL"), index=True, nullable=True
    )
    body: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
