"""notifications (FR-32)

Revision ID: 0004_notifications
Revises: 0003_speech_and_evaluation
Create Date: 2026-08-20

Purpose:    fourth revision: the in-app inbox table.
Why needed: when a report is ready the user must be told; storing notifications
            durably means they survive a restart and show on next login (FR-32).
Contains:
    upgrade   - creates `notifications` and its indexes.
    downgrade - drops it.
Used by:    Alembic (`alembic upgrade head`).
Depends on: 0003_speech_and_evaluation (chain order), `users` and `gd_sessions`
            (foreign keys). Mirrors notifications/models.py.
"""

from collections.abc import Sequence  # type of the optional Alembic revision links

import sqlalchemy as sa  # column and constraint types
from alembic import op  # migration operations (create_table, add_column, ...)

# Alembic revision identifiers: this revision, its parent, and optional branch info.
revision: str = "0004_notifications"
down_revision: str | None = "0003_speech_and_evaluation"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision: create the tables/columns described in the module docstring."""
    # notifications: one inbox message per user (kind, text, optional link, read time).
    op.create_table(
        "notifications",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("gd_sessions.id", ondelete="CASCADE"),
            nullable=True,
        ),
        sa.Column("kind", sa.String(length=32), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("link", sa.String(length=300), nullable=True),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("read_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_notifications_user_id", "notifications", ["user_id"])
    op.create_index("ix_notifications_session_id", "notifications", ["session_id"])
    # The list endpoint always orders by recency.
    op.create_index("ix_notifications_created_at", "notifications", ["created_at"])


def downgrade() -> None:
    """Undo this revision (reverse order of upgrade)."""
    op.drop_index("ix_notifications_created_at", table_name="notifications")
    op.drop_index("ix_notifications_session_id", table_name="notifications")
    op.drop_index("ix_notifications_user_id", table_name="notifications")
    op.drop_table("notifications")
