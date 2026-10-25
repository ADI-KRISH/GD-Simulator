"""auth slice: users, profiles, refresh tokens, password reset tokens

Revision ID: 0001_auth_slice
Revises:
Create Date: 2026-08-20

Purpose:    first database revision: the tables for accounts and sign-in.
Why needed: nothing else can be stored without users; every later table points
            back at `users` (sessions, participants, scores, notifications).
Contains:
    upgrade   - creates `users`, `profiles`, `refresh_tokens`,
                `password_reset_tokens` with their indexes.
    downgrade - drops them again in reverse order.
Used by:    Alembic (run through `alembic upgrade head` by the launchers).
Depends on: nothing; it is the root revision. Mirrors auth/models.py.
"""

from collections.abc import Sequence  # type of the optional Alembic revision links

import sqlalchemy as sa  # column and constraint types
from alembic import op  # migration operations (create_table, add_column, ...)

# Alembic revision identifiers: this revision, its parent, and optional branch info.
revision: str = "0001_auth_slice"
down_revision: str | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    """Apply this revision: create the tables/columns described in the module docstring."""
    # users: one row per account; email is unique (login id) and role decides rights.
    op.create_table(
        "users",
        sa.Column("id", sa.Uuid(), primary_key=True, nullable=False),
        sa.Column("email", sa.String(length=320), nullable=False),
        sa.Column("full_name", sa.String(length=200), nullable=False),
        sa.Column("password_hash", sa.String(length=200), nullable=False),
        sa.Column("role", sa.String(length=20), nullable=False, server_default="candidate"),
        sa.Column("is_active", sa.Boolean(), nullable=False, server_default=sa.true()),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_users_email", "users", ["email"], unique=True)

    # profiles: per-user learning profile (skills, strengths, progress), 1:1 with users.
    op.create_table(
        "profiles",
        sa.Column(
            "user_id",
            sa.Uuid(),
            sa.ForeignKey("users.id", ondelete="CASCADE"),
            primary_key=True,
            nullable=False,
        ),
        sa.Column("skills", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column(
            "experience_level", sa.String(length=20), nullable=False, server_default="student"
        ),
        sa.Column("strengths", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("weaknesses", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("improvement_areas", sa.JSON(), nullable=False, server_default="[]"),
        sa.Column("sessions_completed", sa.Integer(), nullable=False, server_default="0"),
        sa.Column(
            "updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )

    # refresh_tokens: one row per issued refresh token so it can be revoked (logout).
    op.create_table(
        "refresh_tokens",
        sa.Column("jti", sa.String(length=36), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_refresh_tokens_user_id", "refresh_tokens", ["user_id"])

    # password_reset_tokens: only the HASH of each reset token is stored, never the token.
    op.create_table(
        "password_reset_tokens",
        sa.Column("token_hash", sa.String(length=64), primary_key=True, nullable=False),
        sa.Column(
            "user_id", sa.Uuid(), sa.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
        ),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("used_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column(
            "created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False
        ),
    )
    op.create_index("ix_password_reset_tokens_user_id", "password_reset_tokens", ["user_id"])


def downgrade() -> None:
    """Undo this revision (reverse order of upgrade)."""
    # Drop children before `users` so foreign keys never point at a missing table.
    op.drop_index("ix_password_reset_tokens_user_id", table_name="password_reset_tokens")
    op.drop_table("password_reset_tokens")
    op.drop_index("ix_refresh_tokens_user_id", table_name="refresh_tokens")
    op.drop_table("refresh_tokens")
    op.drop_table("profiles")
    op.drop_index("ix_users_email", table_name="users")
    op.drop_table("users")
