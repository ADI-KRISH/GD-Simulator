"""GD session, participant and lobby tables (FR-03 – FR-07, design §7.4, §7.5).

Postgres holds the durable record. Redis holds only the volatile projection of
lobby occupancy and timers (design §35) — never the source of truth.
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
    Boolean,
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
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import JSON

from services.common.db import Base
from services.session.state import ParticipantState, SessionState


class GDType(StrEnum):
    """Drives dynamic agent selection in the Supervisor (design §22)."""

    TECHNICAL = "technical"
    LEADERSHIP = "leadership"
    HR_SOCIAL = "hr_social"
    GENERAL = "general"


class Difficulty(StrEnum):
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"


# The default rubric from claude.md "Score weights". Admin-configurable per
# session; the aggregator reads whatever is stored on the row.
DEFAULT_WEIGHTS: dict[str, float] = {
    "content": 0.30,
    "communication": 0.20,
    "leadership": 0.20,
    "collaboration": 0.20,
    "behavior": 0.10,
}
DEFAULT_RUBRIC_VERSION = "1.0"


class GDSession(Base):
    """FR-03. One configured group discussion."""

    __tablename__ = "gd_sessions"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    # Short human-typable code used to join (FR-05).
    code: Mapped[str] = mapped_column(String(12), unique=True, index=True, nullable=False)
    host_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )

    topic: Mapped[str] = mapped_column(Text, nullable=False)
    topic_category: Mapped[str] = mapped_column(String(100), default="general", nullable=False)
    gd_type: Mapped[str] = mapped_column(String(20), default=GDType.GENERAL, nullable=False)
    difficulty: Mapped[str] = mapped_column(
        String(20), default=Difficulty.INTERMEDIATE, nullable=False
    )

    min_participants: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    max_participants: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    duration_seconds: Mapped[int] = mapped_column(Integer, default=900, nullable=False)
    preparation_seconds: Mapped[int] = mapped_column(Integer, default=120, nullable=False)

    video_required: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    recording_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # Rubric travels with the session so a score stays reproducible even if the
    # platform default changes later (design §40).
    rubric_weights: Mapped[dict] = mapped_column(
        JSON, default=lambda: dict(DEFAULT_WEIGHTS), nullable=False
    )
    rubric_version: Mapped[str] = mapped_column(
        String(20), default=DEFAULT_RUBRIC_VERSION, nullable=False
    )

    state: Mapped[str] = mapped_column(
        String(20), default=SessionState.CREATED, index=True, nullable=False
    )
    # Preparation begins at STARTING; the discussion clock is separate.
    preparation_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    # Set when the session goes LIVE; every transcript timestamp is relative to it.
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failure_reason: Mapped[str | None] = mapped_column(Text, nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    participants: Mapped[list["Participant"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="Participant.joined_at",
    )

    @property
    def is_live(self) -> bool:
        return self.state == SessionState.LIVE


class Participant(Base):
    """FR-05 – FR-07, design §9. One candidate's membership in one session."""

    __tablename__ = "participants"
    __table_args__ = (
        UniqueConstraint("session_id", "user_id", name="uq_participant_session_user"),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("gd_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="CASCADE"), index=True, nullable=False
    )

    # Stable short label ("A", "B", ...) used everywhere downstream: transcript
    # segments, metrics, evidence packets and reports. Assigned on join so the
    # AI layer never has to handle raw UUIDs or real names.
    candidate_label: Mapped[str] = mapped_column(String(4), nullable=False)
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)

    state: Mapped[str] = mapped_column(String(20), default=ParticipantState.JOINED, nullable=False)
    is_ready: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    joined_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    left_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    # Cumulative, survives reconnects; the metrics engine recomputes from speech
    # events rather than trusting this (invariant 4) — it is for the live UI.
    connection_drops: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    last_seen_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    session: Mapped[GDSession] = relationship(back_populates="participants")


class SpeechEvent(Base):
    """Raw speech activity (FR-09 SPEECH_STARTED / SPEECH_STOPPED).

    The deterministic metrics engine (design §17) computes everything from these
    rows — speaking time, turns, interruptions, latency. Persisted as they
    arrive, like transcript segments.
    """

    __tablename__ = "speech_events"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("gd_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    candidate_label: Mapped[str] = mapped_column(String(4), index=True, nullable=False)
    # Seconds from session start — same clock as transcript segments.
    start: Mapped[float] = mapped_column(Float, nullable=False)
    end: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
