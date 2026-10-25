"""session mode, per-turn tracking, agent provenance

Revision ID: 0005_modes_and_turns
Revises: 0004_notifications
Create Date: 2026-10-06

Purpose:    fifth revision: adds what solo practice mode and richer evaluation need.
Why needed: practice sessions differ from group ones (`mode`, AI `personas`),
            the metrics need explicit turns and interruptions, and each agent
            output should record how it was produced (`provenance`).
Contains:
    upgrade   - adds `gd_sessions.mode` and `.personas`, `agent_outputs.provenance`,
                and creates the `turns` table with indexes.
    downgrade - removes those columns and the table.
Used by:    Alembic (`alembic upgrade head`).
Depends on: 0004_notifications (chain order); changes tables made in 0002 and 0003.
"""

from collections.abc import Sequence  # type of the optional Alembic revision links

import sqlalchemy as sa  # column and constraint types
from alembic import op  # migration operations (create_table, add_column, ...)

# Alembic revision identifiers: this revision, its parent, and optional branch info.
revision: str = "0005_modes_and_turns"
down_revision: str | None = "0004_notifications"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision: create the tables/columns described in the module docstring."""
    # mode: "group" (humans over LiveKit) or "solo" (practice against AI personas);
    # existing rows default to "group".
    op.add_column(
        "gd_sessions", sa.Column("mode", sa.String(10), nullable=False, server_default="group")
    )
    # personas: which AI personas sit in a solo practice session.
    op.add_column(
        "gd_sessions", sa.Column("personas", sa.JSON(), nullable=False, server_default="[]")
    )
    # provenance: how each agent output was produced (tools used, critic result).
    op.add_column(
        "agent_outputs",
        sa.Column("provenance", sa.JSON(), nullable=False, server_default="{}"),
    )
    # turns: who held the floor when, plus who they interrupted or replied to.
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
    """Undo this revision (reverse order of upgrade)."""
    # Remove the new table first, then the columns, in reverse order of creation.
    op.drop_table("turns")
    op.drop_column("agent_outputs", "provenance")
    op.drop_column("gd_sessions", "personas")
    op.drop_column("gd_sessions", "mode")
