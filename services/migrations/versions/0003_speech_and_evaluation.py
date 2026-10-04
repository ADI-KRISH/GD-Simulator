"""speech + evaluation slices: transcripts, audio, rolling state, jobs, scores, reports

Revision ID: 0003_speech_and_evaluation
Revises: 0002_session_slice
Create Date: 2026-08-20
"""

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op

revision: str = "0003_speech_and_evaluation"
down_revision: str | None = "0002_session_slice"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "transcript_segments",
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
        sa.Column("text", sa.Text(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("chunk_index", sa.Integer(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_transcript_segments_session_id", "transcript_segments", ["session_id"])
    op.create_index(
        "ix_transcript_segments_candidate_label", "transcript_segments", ["candidate_label"]
    )
    op.create_index("ix_transcript_segments_chunk_index", "transcript_segments", ["chunk_index"])

    op.create_table(
        "audio_chunks",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("gd_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("candidate_label", sa.String(length=4), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("start", sa.Float(), nullable=False),
        sa.Column("end", sa.Float(), nullable=False),
        sa.Column("storage_key", sa.String(length=500), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False, server_default="pending"),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "session_id", "candidate_label", "sequence", name="uq_audio_chunk_sequence"
        ),
    )
    op.create_index("ix_audio_chunks_session_id", "audio_chunks", ["session_id"])
    op.create_index("ix_audio_chunks_status", "audio_chunks", ["status"])

    op.create_table(
        "rolling_states",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("gd_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("chunk_index", sa.Integer(), nullable=False),
        sa.Column("state", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("session_id", "chunk_index", name="uq_rolling_state_chunk"),
    )
    op.create_index("ix_rolling_states_session_id", "rolling_states", ["session_id"])

    op.create_table(
        "evaluation_jobs",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("gd_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("evaluation_version", sa.Integer(), nullable=False, server_default="1"),
        sa.Column("state", sa.String(length=24), nullable=False, server_default="QUEUED"),
        sa.Column("gd_type", sa.String(length=20), nullable=False),
        sa.Column("rubric", sa.JSON(), nullable=False),
        sa.Column("rubric_version", sa.String(length=20), nullable=False),
        sa.Column("prompt_version", sa.String(length=20), nullable=False),
        sa.Column("agent_version", sa.String(length=20), nullable=False),
        sa.Column("selected_agents", sa.JSON(), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("error", sa.Text(), nullable=True),
        sa.Column(
            "queued_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        # Design §39: the idempotency key, enforced by the database.
        sa.UniqueConstraint("session_id", "evaluation_version", name="uq_evaluation_job_version"),
    )
    op.create_index("ix_evaluation_jobs_session_id", "evaluation_jobs", ["session_id"])
    op.create_index("ix_evaluation_jobs_state", "evaluation_jobs", ["state"])

    op.create_table(
        "agent_outputs",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("evaluation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("candidate_label", sa.String(length=4), nullable=False),
        sa.Column("dimension", sa.String(length=24), nullable=False),
        sa.Column("agent_name", sa.String(length=40), nullable=False),
        sa.Column("score", sa.Float(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("strengths", sa.JSON(), nullable=False),
        sa.Column("weaknesses", sa.JSON(), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint(
            "job_id", "candidate_label", "dimension", name="uq_agent_output_dimension"
        ),
    )
    op.create_index("ix_agent_outputs_job_id", "agent_outputs", ["job_id"])
    op.create_index("ix_agent_outputs_candidate_label", "agent_outputs", ["candidate_label"])

    op.create_table(
        "candidate_scores",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("evaluation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("candidate_label", sa.String(length=4), nullable=False),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("overall", sa.Float(), nullable=False),
        sa.Column("dimensions", sa.JSON(), nullable=False),
        sa.Column("weights", sa.JSON(), nullable=False),
        sa.Column("confidence", sa.Float(), nullable=False),
        sa.Column("needs_human_review", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("metrics", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.UniqueConstraint("job_id", "candidate_label", name="uq_candidate_score_job"),
    )
    op.create_index("ix_candidate_scores_job_id", "candidate_scores", ["job_id"])
    op.create_index("ix_candidate_scores_user_id", "candidate_scores", ["user_id"])

    op.create_table(
        "reports",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column(
            "job_id",
            sa.Uuid(),
            sa.ForeignKey("evaluation_jobs.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "session_id",
            sa.Uuid(),
            sa.ForeignKey("gd_sessions.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("candidate_label", sa.String(length=4), nullable=True),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
        ),
        sa.Column("body", sa.JSON(), nullable=False),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_reports_job_id", "reports", ["job_id"])
    op.create_index("ix_reports_session_id", "reports", ["session_id"])
    op.create_index("ix_reports_candidate_label", "reports", ["candidate_label"])
    op.create_index("ix_reports_user_id", "reports", ["user_id"])


def downgrade() -> None:
    for index, table in (
        ("ix_reports_user_id", "reports"),
        ("ix_reports_candidate_label", "reports"),
        ("ix_reports_session_id", "reports"),
        ("ix_reports_job_id", "reports"),
    ):
        op.drop_index(index, table_name=table)
    op.drop_table("reports")

    op.drop_index("ix_candidate_scores_user_id", table_name="candidate_scores")
    op.drop_index("ix_candidate_scores_job_id", table_name="candidate_scores")
    op.drop_table("candidate_scores")

    op.drop_index("ix_agent_outputs_candidate_label", table_name="agent_outputs")
    op.drop_index("ix_agent_outputs_job_id", table_name="agent_outputs")
    op.drop_table("agent_outputs")

    op.drop_index("ix_evaluation_jobs_state", table_name="evaluation_jobs")
    op.drop_index("ix_evaluation_jobs_session_id", table_name="evaluation_jobs")
    op.drop_table("evaluation_jobs")

    op.drop_index("ix_rolling_states_session_id", table_name="rolling_states")
    op.drop_table("rolling_states")

    op.drop_index("ix_audio_chunks_status", table_name="audio_chunks")
    op.drop_index("ix_audio_chunks_session_id", table_name="audio_chunks")
    op.drop_table("audio_chunks")

    op.drop_index("ix_transcript_segments_chunk_index", table_name="transcript_segments")
    op.drop_index("ix_transcript_segments_candidate_label", table_name="transcript_segments")
    op.drop_index("ix_transcript_segments_session_id", table_name="transcript_segments")
    op.drop_table("transcript_segments")
