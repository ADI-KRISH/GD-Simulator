"""trace spans for the observability dashboard

Revision ID: 0006_trace_spans
Revises: 0005_modes_and_turns
Create Date: 2026-10-25

Purpose:    sixth revision: the table the tracer writes every model/speech call to.
Contains:
    upgrade   - creates `trace_spans` and its indexes.
    downgrade - drops them.
Used by:    Alembic (`alembic upgrade head`).
Depends on: 0005_modes_and_turns (chain order).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0006_trace_spans"
down_revision: str | None = "0005_modes_and_turns"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Create the span table."""
    op.create_table(
        "trace_spans",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("kind", sa.String(12), nullable=False),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("name", sa.String(60), nullable=False),
        sa.Column("provider", sa.String(20), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("session_id", sa.String(36), nullable=True),
        sa.Column("run", sa.String(40), nullable=True),
        sa.Column("agent", sa.String(40), nullable=True),
        sa.Column("dimension", sa.String(24), nullable=True),
        sa.Column("candidate", sa.String(8), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("ttfb_ms", sa.Float(), nullable=True),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("priced", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(8), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column("system_prompt", sa.Text(), nullable=True),
        sa.Column("prompt", sa.Text(), nullable=True),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("tool_calls", sa.JSON(), nullable=False),
        sa.Column("meta", sa.JSON(), nullable=False),
    )
    for column in ("started_at", "kind", "purpose", "session_id", "agent", "status"):
        op.create_index(f"ix_trace_spans_{column}", "trace_spans", [column])


def downgrade() -> None:
    """Drop the span table."""
    op.drop_table("trace_spans")
