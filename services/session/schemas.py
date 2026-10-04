"""Request/response shapes for the session and lobby APIs."""

import uuid
from datetime import datetime

from pydantic import BaseModel, ConfigDict, EmailStr, Field, field_validator, model_validator

from services.session.models import DEFAULT_WEIGHTS, Difficulty, GDType
from services.session.state import ParticipantState, SessionState

# Dimensions the rubric may weight. Mirrors the evaluation_output contract enum
# minus critical_thinking, which folds into content when aggregating (§31).
RUBRIC_DIMENSIONS = frozenset(DEFAULT_WEIGHTS)


class SessionCreate(BaseModel):
    """FR-03."""

    topic: str = Field(min_length=5, max_length=2000)
    topic_category: str = Field(default="general", max_length=100)
    gd_type: GDType = GDType.GENERAL
    difficulty: Difficulty = Difficulty.INTERMEDIATE
    min_participants: int = Field(default=3, ge=2, le=20)
    max_participants: int = Field(default=8, ge=2, le=20)
    duration_seconds: int = Field(default=900, ge=60, le=7200)
    preparation_seconds: int = Field(default=120, ge=0, le=1800)
    video_required: bool = True
    recording_enabled: bool = False
    rubric_weights: dict[str, float] | None = None

    @model_validator(mode="after")
    def check_capacity(self) -> "SessionCreate":
        if self.min_participants > self.max_participants:
            raise ValueError("min_participants cannot exceed max_participants")
        return self

    @field_validator("rubric_weights")
    @classmethod
    def check_weights(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        if value is None:
            return None
        unknown = set(value) - RUBRIC_DIMENSIONS
        if unknown:
            raise ValueError(f"unknown rubric dimensions: {', '.join(sorted(unknown))}")
        missing = RUBRIC_DIMENSIONS - set(value)
        if missing:
            raise ValueError(f"missing rubric dimensions: {', '.join(sorted(missing))}")
        total = sum(value.values())
        # Deterministic aggregation depends on a normalised policy (§31).
        if abs(total - 1.0) > 1e-6:
            raise ValueError(f"rubric weights must sum to 1.0 (got {total:.4f})")
        return value


class SessionUpdate(BaseModel):
    """Host edits, allowed only before the session starts."""

    topic: str | None = Field(default=None, min_length=5, max_length=2000)
    topic_category: str | None = Field(default=None, max_length=100)
    gd_type: GDType | None = None
    difficulty: Difficulty | None = None
    duration_seconds: int | None = Field(default=None, ge=60, le=7200)
    preparation_seconds: int | None = Field(default=None, ge=0, le=1800)
    video_required: bool | None = None
    recording_enabled: bool | None = None


class ParticipantOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    candidate_label: str
    display_name: str
    state: ParticipantState
    is_ready: bool
    joined_at: datetime


class SessionOut(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    host_id: uuid.UUID
    topic: str
    topic_category: str
    gd_type: GDType
    difficulty: Difficulty
    min_participants: int
    max_participants: int
    duration_seconds: int
    preparation_seconds: int
    video_required: bool
    recording_enabled: bool
    rubric_weights: dict[str, float]
    rubric_version: str
    state: SessionState
    started_at: datetime | None
    ended_at: datetime | None
    created_at: datetime
    participants: list[ParticipantOut] = Field(default_factory=list)


class SessionSummary(BaseModel):
    """List view — no participant fan-out."""

    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    code: str
    topic: str
    gd_type: GDType
    state: SessionState
    duration_seconds: int
    created_at: datetime
    started_at: datetime | None


class JoinRequest(BaseModel):
    """FR-05: join by session code."""

    code: str = Field(min_length=4, max_length=12)


class InviteRequest(BaseModel):
    """FR-05: invite a registered candidate by email."""

    email: EmailStr


class ReadyRequest(BaseModel):
    """FR-07."""

    ready: bool = True


class LobbyView(BaseModel):
    """FR-04: what the lobby screen renders."""

    lobby_id: uuid.UUID
    session_id: uuid.UUID
    code: str
    topic: str
    session_state: SessionState
    capacity: int
    min_participants: int
    occupancy: int
    ready_count: int
    start_condition_met: bool
    participants: list[ParticipantOut]


class TimerView(BaseModel):
    """FR-10. Server-authoritative clock; clients render, never decide."""

    session_id: uuid.UUID
    phase: str
    seconds_remaining: int
    duration_seconds: int
    warning: bool


class TopicView(BaseModel):
    """FR-11. Only released once the session is past preparation."""

    session_id: uuid.UUID
    topic: str
    topic_category: str
    difficulty: Difficulty
    released: bool


class MediaToken(BaseModel):
    """FR-08. LiveKit join credentials for the media plane."""

    url: str
    token: str
    room: str
    identity: str
