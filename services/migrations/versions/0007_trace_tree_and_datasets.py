"""trace parent links, dataset cases and experiment results

Revision ID: 0007_trace_tree_and_datasets
Revises: 0006_trace_spans
Create Date: 2026-10-26

Purpose:    seventh revision: lets a call point at the chain it ran inside, and adds
            the tables for saved test cases and the results of replaying them.
Contains:
    upgrade   - adds `trace_spans.parent_id`; creates `eval_cases` and `eval_results`.
    downgrade - removes them.
Used by:    Alembic (`alembic upgrade head`).
Depends on: 0006_trace_spans (chain order).
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0007_trace_tree_and_datasets"
down_revision: str | None = "0006_trace_spans"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Add the parent link and the two dataset tables."""
    op.add_column("trace_spans", sa.Column("parent_id", sa.Uuid(), nullable=True))
    op.create_index("ix_trace_spans_parent_id", "trace_spans", ["parent_id"])

    op.create_table(
        "eval_cases",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("dataset", sa.String(60), nullable=False),
        sa.Column("source_span_id", sa.Uuid(), nullable=True),
        sa.Column("purpose", sa.String(24), nullable=False),
        sa.Column("agent", sa.String(40), nullable=True),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("system_prompt", sa.Text(), nullable=False),
        sa.Column("prompt", sa.Text(), nullable=False),
        sa.Column("schema", sa.JSON(), nullable=False),
        sa.Column("expected", sa.JSON(), nullable=False),
        sa.Column("note", sa.Text(), nullable=False),
    )
    op.create_index("ix_eval_cases_dataset", "eval_cases", ["dataset"])

    op.create_table(
        "eval_results",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("run_id", sa.String(36), nullable=False),
        sa.Column("dataset", sa.String(60), nullable=False),
        sa.Column("label", sa.String(60), nullable=False),
        sa.Column("case_id", sa.Uuid(), nullable=False),
        sa.Column("model", sa.String(80), nullable=False),
        sa.Column("output", sa.JSON(), nullable=True),
        sa.Column("match", sa.Float(), nullable=True),
        sa.Column("latency_ms", sa.Float(), nullable=False),
        sa.Column("input_tokens", sa.Integer(), nullable=False),
        sa.Column("output_tokens", sa.Integer(), nullable=False),
        sa.Column("cost_usd", sa.Float(), nullable=False),
        sa.Column("error", sa.Text(), nullable=True),
    )
    for column in ("run_id", "dataset", "case_id"):
        op.create_index(f"ix_eval_results_{column}", "eval_results", [column])


def downgrade() -> None:
    """Remove what upgrade added."""
    op.drop_table("eval_results")
    op.drop_table("eval_cases")
    op.drop_index("ix_trace_spans_parent_id", table_name="trace_spans")
    op.drop_column("trace_spans", "parent_id")
