"""session mode, per-turn tracking, agent provenance

Revision ID: 0005_modes_and_turns
Revises: 0004_notifications
Create Date: 2026-10-06
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0005_modes_and_turns"
down_revision: str | None = "0004_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "gd_sessions", sa.Column("mode", sa.String(10), nullable=False, server_default="group")
    )
    op.add_column(
        "gd_sessions", sa.Column("personas", sa.JSON(), nullable=False, server_default="[]")
    )
    op.add_column(
        "agent_outputs",
        sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"),
    )
    op.create_table(
        "turns",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("gd_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("candidate_label", sa.String(4), nullable=False),
        sa.Column("start", sa.Float(), nullable=False),
        sa.Column("end", sa.Float(), nullable=False),
        sa.Column("interrupted_label", sa.String(4), nullable=True),
        sa.Column("reply_to_label", sa.String(4), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_turns_session_id", "turns", ["session_id"])
    op.create_index("ix_turns_candidate_label", "turns", ["candidate_label"])


def downgrade() -> None:
    op.drop_table("turns")
    op.drop_column("agent_outputs", "provenance")
    op.drop_column("gd_sessions", "personas")
    op.drop_column("gd_sessions", "mode")
