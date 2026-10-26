"""GD session, participant and lobby tables (FR-03 – FR-07, design §7.4, §7.5).

Postgres holds the durable record. Redis holds only the volatile projection of
lobby occupancy and timers (design §35) — never the source of truth.

Purpose: SQLAlchemy tables for group discussions: the session itself, its participants, raw
    speech events and tracked turns.
Why needed: Postgres is the durable source of truth for who took part in what and when; the
    metrics engine, transcript and reports all key off these rows (Redis only holds a volatile
    projection).
Contains:
  - GDType / Difficulty: enums that pick the evaluator agents and the topic difficulty.
  - DEFAULT_WEIGHTS / DEFAULT_RUBRIC_VERSION: the default score rubric copied onto each session.
  - GDSession: one configured discussion (topic, limits, rubric, state, timestamps, mode,
    personas).
  - Participant: one candidate's membership, with a stable label (A, B, ...) used downstream.
  - SpeechEvent: raw start/stop of speech that the deterministic metrics engine computes from.
  - Turn: one tracked turn, who they interrupted and who they replied to, for live display.
Used by: services/evaluation/agents/definitions.py, services/evaluation/service.py,
    services/observer/service.py, services/practice/engine.py, services/practice/router.py,
    services/session/repository.py, services/session/schemas.py, services/session/service.py,
    services/speech/service.py, services/tests/test_aggregator.py,
    services/tests/test_evaluation_api.py, services/tests/test_evaluation_pipeline.py,
    services/tests/test_langgraph_engine.py, services/tests/test_multiplayer_rules.py,
    services/tests/test_synthesis.py
Depends on: services/common/db.py, services/session/state.py
"""

import uuid  # UUID primary keys
from datetime import datetime  # timestamp column types
from enum import StrEnum  # string enums stored as plain text

from sqlalchemy import (  # column and constraint types
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
from sqlalchemy.orm import Mapped, mapped_column, relationship  # typed ORM declarations
from sqlalchemy.types import JSON  # rubric weights, personas

from services.common.db import Base  # declarative base
from services.session.state import ParticipantState, SessionState  # default/typed states


class GDType(StrEnum):
    """Drives dynamic agent selection in the Supervisor (design §22)."""

    TECHNICAL = "technical"
    LEADERSHIP = "leadership"
    HR_SOCIAL = "hr_social"
    GENERAL = "general"


class Difficulty(StrEnum):
    """How demanding the topic and AI personas should be."""

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
    # RESTRICT: a host account cannot be deleted while it still owns sessions.
    host_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("users.id", ondelete="RESTRICT"), index=True, nullable=False
    )

    # What the group will discuss.
    topic: Mapped[str] = mapped_column(Text, nullable=False)
    topic_category: Mapped[str] = mapped_column(String(100), default="general", nullable=False)
    # Decides which evaluator agents the Supervisor runs (technical, leadership, hr_social,
    # general).
    gd_type: Mapped[str] = mapped_column(String(20), default=GDType.GENERAL, nullable=False)
    difficulty: Mapped[str] = mapped_column(
        String(20), default=Difficulty.INTERMEDIATE, nullable=False
    )

    # Lobby size limits: the session cannot start until enough people are ready.
    min_participants: Mapped[int] = mapped_column(Integer, default=3, nullable=False)
    max_participants: Mapped[int] = mapped_column(Integer, default=8, nullable=False)
    # Length of the live discussion, separate from the preparation window below.
    duration_seconds: Mapped[int] = mapped_column(Integer, default=900, nullable=False)
    preparation_seconds: Mapped[int] = mapped_column(Integer, default=120, nullable=False)

    # Room options chosen by the host when the session is created.
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

    # "group" = students in a lobby; "solo" = one student vs AI personas. Both
    # share every downstream table and the whole evaluation pipeline.
    mode: Mapped[str] = mapped_column(String(10), default="group", nullable=False)
    # Solo practice only: the AI personas in the room, [{label, name, persona,
    # voice_id, style}]. Lives on the row so a restart can rebuild the room.
    personas: Mapped[list] = mapped_column(JSON, default=list, nullable=False)

    # Current lifecycle state (SessionState); indexed because lobbies and workers filter by it.
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

    # Loaded together with the session and ordered by join time, so rosters list people in a stable
    # order.
    participants: Mapped[list["Participant"]] = relationship(
        back_populates="session",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="Participant.joined_at",
    )

    @property
    def is_live(self) -> bool:
        """True while the discussion is running."""
        return self.state == SessionState.LIVE


class Participant(Base):
    """FR-05 – FR-07, design §9. One candidate's membership in one session."""

    __tablename__ = "participants"
    # A user can be in a session only once; this constraint is what makes re-joining idempotent.
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

    # Participant lifecycle (ParticipantState): starts as JOINED when someone enters.
    state: Mapped[str] = mapped_column(String(20), default=ParticipantState.JOINED, nullable=False)
    # Readiness flag shown to the host and the other participants in the lobby.
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
    # Who spoke, by anonymous label rather than user id.
    candidate_label: Mapped[str] = mapped_column(String(4), index=True, nullable=False)
    # Seconds from session start — same clock as transcript segments.
    start: Mapped[float] = mapped_column(Float, nullable=False)
    end: Mapped[float] = mapped_column(Float, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class Turn(Base):
    """One tracked turn: who spoke, whom they interrupted, whom they replied to.

    Written live as each speech event lands (humans and AI personas alike), so
    interruptions and replies are queryable mid-session. The metrics engine still
    recomputes from `speech_events` — this table is for live display, the
    interruption penalty's evidence trail, and the evaluators' tools.
    """

    __tablename__ = "turns"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("gd_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    # Who held this turn, by label.
    candidate_label: Mapped[str] = mapped_column(String(4), index=True, nullable=False)
    start: Mapped[float] = mapped_column(Float, nullable=False)
    end: Mapped[float] = mapped_column(Float, nullable=False)
    # Set when this speaker started while someone else still held the floor.
    interrupted_label: Mapped[str | None] = mapped_column(String(4), nullable=True)
    # Set when this speaker followed another within the response window.
    reply_to_label: Mapped[str | None] = mapped_column(String(4), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @property
    def is_interruption(self) -> bool:
        """True if this turn started while another speaker still had the floor."""
        return self.interrupted_label is not None
