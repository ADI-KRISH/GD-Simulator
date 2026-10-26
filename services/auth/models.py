"""User, profile and token tables (FR-01, FR-02).

Purpose: SQLAlchemy tables for accounts: users, their profiles, refresh tokens and
    password-reset tokens, plus the Role and ExperienceLevel enums.
Why needed: This is the durable record of who can sign in and with what permissions; sessions,
    lobbies and reports all point back to users.id.
Contains:
  - Role: candidate / host / admin, the permission levels checked by require_role.
  - ExperienceLevel: self-declared career stage shown on a profile.
  - User: login identity, bcrypt password hash, role and active flag.
  - Profile: one-to-one extra details (skills, strengths, weaknesses) editable by the user.
  - RefreshToken: one row per issued refresh token so logout can really revoke a session.
  - PasswordResetToken: single-use reset token stored only as a hash.
Used by: services/ai_platform/bridge.py, services/auth/deps.py, services/auth/repository.py,
    services/auth/schemas.py, services/auth/service.py, services/evaluation/router.py,
    services/session/repository.py, services/session/service.py, services/simulation.py,
    services/speech/router.py
Depends on: services/common/db.py
"""

import uuid  # UUID primary keys
from datetime import datetime  # timestamp column types
from enum import StrEnum  # string enums stored as plain text

from sqlalchemy import Boolean, DateTime, ForeignKey, Integer, String, Uuid, func  # column types
from sqlalchemy.orm import Mapped, mapped_column, relationship  # typed ORM declarations
from sqlalchemy.types import JSON  # list columns (skills, strengths, ...)

from services.common.db import Base  # declarative base for all tables


class Role(StrEnum):
    """What a user may do: take part, host sessions, or administer."""

    CANDIDATE = "candidate"
    HOST = "host"
    ADMIN = "admin"


class ExperienceLevel(StrEnum):
    """Self-declared career stage shown on the profile."""

    STUDENT = "student"
    ENTRY = "entry"
    MID = "mid"
    SENIOR = "senior"


class User(Base):
    """An account: login identity, hashed password and role."""

    __tablename__ = "users"

    # Random UUID primary key so account ids cannot be guessed or enumerated.
    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Unique and indexed: login looks accounts up by email.
    email: Mapped[str] = mapped_column(String(320), unique=True, index=True, nullable=False)
    full_name: Mapped[str] = mapped_column(String(200), nullable=False)
    password_hash: Mapped[str] = mapped_column(String(200), nullable=False)  # bcrypt hash
    # Stored as text; defaults to the least-privileged role.
    role: Mapped[str] = mapped_column(String(20), default=Role.CANDIDATE, nullable=False)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)  # False = off
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    # One-to-one; selectin-loaded so reading a user never needs a second lazy query.
    profile: Mapped["Profile"] = relationship(
        back_populates="user", uselist=False, cascade="all, delete-orphan", lazy="selectin"
    )


class Profile(Base):
    """FR-02. Session history and historical scores are derived by later slices
    from the sessions/evaluations tables, not stored here."""

    __tablename__ = "profiles"

    # The profile reuses the user id as its primary key, which makes the relation strictly
    # one-to-one.
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), primary_key=True
    )
    # List-valued profile fields are JSON columns; they are free-form and user editable.
    skills: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    experience_level: Mapped[str] = mapped_column(
        String(20), default=ExperienceLevel.STUDENT, nullable=False
    )
    strengths: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    weaknesses: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    improvement_areas: Mapped[list[str]] = mapped_column(JSON, default=list, nullable=False)
    # Counter kept on the profile for quick display.
    sessions_completed: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    user: Mapped[User] = relationship(back_populates="profile")


class RefreshToken(Base):
    """One row per issued refresh token so logout can actually revoke a session."""

    __tablename__ = "refresh_tokens"

    # The token id from the JWT, so a presented token can be matched to its row.
    jti: Mapped[str] = mapped_column(String(36), primary_key=True)  # JWT id from the token
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Kept so expired rows can be ignored or cleaned up.
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class PasswordResetToken(Base):
    """A single-use password-reset token, stored only as a hash."""

    __tablename__ = "password_reset_tokens"

    # Only the hash is stored, so a database leak does not expose usable reset links.
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)  # SHA-256 hex
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
