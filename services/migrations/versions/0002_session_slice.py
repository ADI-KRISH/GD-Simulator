"""session slice: gd sessions, participants, speech events

Revision ID: 0002_session_slice
Revises: 0001_auth_slice
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0002_session_slice"
down_revision: str | None = "0001_auth_slice"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "gd_sessions",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("code", sa.String(length=12), nullable=False),
        sa.Column(
            "host_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="RESTRICT"), nullable=False
        ),
        sa.Column("topic", sa.Text(), nullable=False),
        sa.Column(
            "topic_category", sa.String(length=100), nullable=False, server_default="general"
        ),
        sa.Column("gd_type", sa.String(length=20), nullable=False, server_default="general"),
        sa.Column(
            "difficulty", sa.String(length=20), nullable=False, server_default="intermediate"
        ),
        sa.Column("min_participants", sa.Integer(), nullable=False, server_default="3"),
        sa.Column("max_participants", sa.Integer(), nullable=False, server_default="8"),
        sa.Column("duration_seconds", sa.Integer(), nullable=False, server_default="900"),
        sa.Column("preparation_seconds", sa.Integer(), nullable=False, server_default="120"),
        sa.Column("video_required", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column("recording_enabled", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("rubric_weights", sa.JSON(), nullable=False),
        sa.Column("rubric_version", sa.String(length=20), nullable=False, server_default="1.0"),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="CREATED"),
        sa.Column("preparation_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("ended_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failure_reason", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_gd_sessions_code", "gd_sessions", ["code"], unique=True)
    op.create_index("ix_gd_sessions_host_id", "gd_sessions", ["host_id"])
    # The timer ticker scans by state every second; without this it table-scans.
    op.create_index("ix_gd_sessions_state", "gd_sessions", ["state"])

    op.create_table(
        "participants",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("gd_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("candidate_label", sa.String(length=4), nullable=False),
        sa.Column("display_name", sa.String(length=200), nullable=False),
        sa.Column("state", sa.String(length=20), nullable=False, server_default="JOINED"),
        sa.Column("is_ready", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column(
            "joined_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("left_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("connection_drops", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=True),
        sa.UniqueConstraint("session_id", "user_id", name="uq_participant_session_user"),
    )
    op.create_index("ix_participants_session_id", "participants", ["session_id"])
    op.create_index("ix_participants_user_id", "participants", ["user_id"])

    op.create_table(
        "speech_events",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("gd_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("candidate_label", sa.String(length=4), nullable=False),
        sa.Column("start", sa.Float(), nullable=False),
        sa.Column("end", sa.Float(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_speech_events_session_id", "speech_events", ["session_id"])
    op.create_index("ix_speech_events_candidate_label", "speech_events", ["candidate_label"])


def downgrade() -> None:
    op.drop_index("ix_speech_events_candidate_label", table_name="speech_events")
    op.drop_index("ix_speech_events_session_id", table_name="speech_events")
    op.drop_table("speech_events")
    op.drop_index("ix_participants_user_id", table_name="participants")
    op.drop_index("ix_participants_session_id", table_name="participants")
    op.drop_table("participants")
    op.drop_index("ix_gd_sessions_state", table_name="gd_sessions")
    op.drop_index("ix_gd_sessions_host_id", table_name="gd_sessions")
    op.drop_index("ix_gd_sessions_code", table_name="gd_sessions")
    op.drop_table("gd_sessions")
