"""Request/response shapes for the session and lobby APIs.

Purpose: Pydantic models that validate what the session and lobby APIs accept and shape what
    they return.
Why needed: Bad input (a lobby that can never start, rubric weights that do not sum to 1) is
    rejected at the API boundary, and the response models keep the API stable even if the tables
    change.
Contains:
  - RUBRIC_DIMENSIONS: the dimension names a custom rubric may weight.
  - SessionCreate / SessionUpdate: request bodies, with validators for capacity and rubric
    weights.
  - ParticipantOut / SessionOut / SessionSummary: participant, full-session and list views.
  - JoinRequest / InviteRequest / ReadyRequest: lobby actions (FR-05, FR-07).
  - LobbyView / LobbyListItem: the lobby screen and the public lobby browser (FR-04).
  - TimerView / TopicView: the server-authoritative clock and the delayed topic (FR-10, FR-11).
  - MediaToken: LiveKit join credentials (FR-08).
  - TurnOut: one tracked speaking turn.
Used by: services/lobby/router.py, services/practice/router.py, services/session/router.py,
    services/session/service.py, services/simulation.py
Depends on: services/session/models.py, services/session/state.py
"""

import uuid  # id types
from datetime import datetime  # timestamps
from typing import Literal  # restrict mode to "group" | "solo"

from pydantic import (  # validated API models and validators
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

from services.session.models import DEFAULT_WEIGHTS, Difficulty, GDType  # defaults and enums
from services.session.state import ParticipantState, SessionState  # state enums

# Dimensions the rubric may weight. Mirrors the evaluation_output contract enum
# minus critical_thinking, which folds into content when aggregating (§31).
RUBRIC_DIMENSIONS = frozenset(DEFAULT_WEIGHTS)


class SessionCreate(BaseModel):
    """FR-03."""

    # Length bounds stop empty or absurdly long topics at the API boundary.
    topic: str = Field(min_length=5, max_length=2000)
    topic_category: str = Field(default="general", max_length=100)
    gd_type: GDType = GDType.GENERAL
    difficulty: Difficulty = Difficulty.INTERMEDIATE
    # Lobby size and timing limits; the validators below check the combinations.
    min_participants: int = Field(default=3, ge=1, le=20)
    max_participants: int = Field(default=8, ge=1, le=20)
    # Discussion length from 1 to 120 minutes; preparation may be 0 to go live straight away.
    duration_seconds: int = Field(default=900, ge=60, le=7200)
    preparation_seconds: int = Field(default=120, ge=0, le=1800)
    video_required: bool = True
    recording_enabled: bool = False
    # Optional custom rubric; None means use DEFAULT_WEIGHTS.
    rubric_weights: dict[str, float] | None = None
    # group means people in a lobby; solo means one student practising against AI personas.
    mode: Literal["group", "solo"] = "group"
    # Solo only: up to four AI participants.
    personas: list[dict] = Field(default_factory=list, max_length=4)

    @model_validator(mode="after")
    def check_capacity(self) -> "SessionCreate":
        """Group sessions need >= 2 people and min cannot exceed max."""
        # Group discussions need at least two people; solo practice is exempt.
        if self.mode == "group" and self.min_participants < 2:
            raise ValueError("a group discussion needs at least 2 participants")
        # A lobby that can never reach its own minimum could never start.
        if self.min_participants > self.max_participants:
            raise ValueError("min_participants cannot exceed max_participants")
        return self

    @field_validator("rubric_weights")
    @classmethod
    def check_weights(cls, value: dict[str, float] | None) -> dict[str, float] | None:
        """Custom rubric weights must name exactly the known dimensions and sum to 1."""
        # None means no custom rubric, so the default applies.
        if value is None:
            return None
        # Reject typos and dimensions the aggregator does not know.
        unknown = set(value) - RUBRIC_DIMENSIONS
        if unknown:
            raise ValueError(f"unknown rubric dimensions: {', '.join(sorted(unknown))}")
        # Every dimension needs a weight, otherwise the formula would silently drop it.
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
    """A participant as shown in the lobby and session views."""

    # Build straight from the Participant ORM object.
    model_config = ConfigDict(from_attributes=True)

    id: uuid.UUID
    user_id: uuid.UUID
    candidate_label: str
    display_name: str
    state: ParticipantState
    is_ready: bool
    joined_at: datetime


class SessionOut(BaseModel):
    """Full session detail including participants."""

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
    mode: str = "group"
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

    # Bounds match the session.code column (up to 12 characters).
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


class LobbyListItem(BaseModel):
    """One open lobby in the public browser."""

    session_id: uuid.UUID
    code: str
    topic: str
    topic_category: str
    gd_type: GDType
    difficulty: Difficulty
    host_name: str
    occupancy: int
    capacity: int
    duration_seconds: int
    preparation_seconds: int


class TurnOut(BaseModel):
    """One tracked speaking turn, with who it interrupted or replied to."""

    model_config = ConfigDict(from_attributes=True)

    candidate_label: str
    start: float
    end: float
    interrupted_label: str | None
    reply_to_label: str | None
