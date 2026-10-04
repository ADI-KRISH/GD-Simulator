"""Transcript storage (FR-15, design §13).

Segments are written the moment they exist, one row each. There is deliberately
no "finalise transcript at session end" step: the raw transcript is the source
of truth and must survive a server, client, network or worker failure at any
point (claude.md invariant 2).
"""

import uuid
from datetime import datetime
from enum import StrEnum

from sqlalchemy import (
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
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy.types import JSON

from services.common.db import Base


class AudioChunkStatus(StrEnum):
    """STT failure is retryable: the raw audio is kept and the segment
    reprocessed (design §38)."""

    PENDING = "pending"
    TRANSCRIBED = "transcribed"
    FAILED = "failed"


class TranscriptSegmentRow(Base):
    """One speaker-attributed, timestamped unit of speech.

    Mirrors `transcript_segment.schema.json`. `start`/`end` are seconds from the
    moment the session went LIVE — the same clock as `speech_events`.
    """

    __tablename__ = "transcript_segments"

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("gd_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    candidate_label: Mapped[str] = mapped_column(String(4), index=True, nullable=False)
    start: Mapped[float] = mapped_column(Float, nullable=False)
    end: Mapped[float] = mapped_column(Float, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    confidence: Mapped[float] = mapped_column(Float, nullable=False)

    # Which processing window this segment fell into (design §14.2). Null until
    # the chunker claims it, so the Observer can find unprocessed speech.
    chunk_index: Mapped[int | None] = mapped_column(Integer, index=True, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class AudioChunk(Base):
    """Raw audio kept for retry and reprocessing (FR-12, design §38).

    The object store holds the bytes; this row holds the pointer and the STT
    outcome, so a failed transcription can be replayed without the browser
    still being connected.
    """

    __tablename__ = "audio_chunks"
    __table_args__ = (
        # The browser retries uploads on a flaky network; the same sequence
        # number from the same speaker must not transcribe twice.
        UniqueConstraint(
            "session_id", "candidate_label", "sequence", name="uq_audio_chunk_sequence"
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("gd_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    candidate_label: Mapped[str] = mapped_column(String(4), nullable=False)
    sequence: Mapped[int] = mapped_column(Integer, nullable=False)
    start: Mapped[float] = mapped_column(Float, nullable=False)
    end: Mapped[float] = mapped_column(Float, nullable=False)

    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), default=AudioChunkStatus.PENDING, index=True, nullable=False
    )
    attempts: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )


class RollingStateRow(Base):
    """Observer output, one row per update (design §14.4, §15).

    History is kept rather than overwritten: the final evaluation reads the
    latest state, but keeping every version makes the session's evolution
    inspectable and lets a bad Observer run be diagnosed after the fact.
    """

    __tablename__ = "rolling_states"
    __table_args__ = (UniqueConstraint("session_id", "chunk_index", name="uq_rolling_state_chunk"),)

    id: Mapped[uuid.UUID] = mapped_column(Uuid, primary_key=True, default=uuid.uuid4)
    session_id: Mapped[uuid.UUID] = mapped_column(
        Uuid, ForeignKey("gd_sessions.id", ondelete="CASCADE"), index=True, nullable=False
    )
    chunk_index: Mapped[int] = mapped_column(Integer, nullable=False)
    # The full RollingState contract payload.
    state: Mapped[dict] = mapped_column(JSON, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
