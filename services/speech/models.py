"""Transcript storage (FR-15, design §13).

Segments are written the moment they exist, one row each. There is deliberately
no "finalise transcript at session end" step: the raw transcript is the source
of truth and must survive a server, client, network or worker failure at any
point (claude.md invariant 2).

Purpose: SQLAlchemy tables for the speech pipeline: transcript segments, uploaded audio chunks
    and the Observer's rolling states.
Why needed: The raw transcript is the source of truth and each segment is saved the moment it
    exists (invariant 2); audio chunks keep a pointer to the stored bytes so failed
    transcriptions can be retried, and rolling states keep the Observer's history.
Contains:
  - AudioChunkStatus: pending, transcribed or failed, so STT failures are retryable.
  - TranscriptSegmentRow: one speaker-attributed, timestamped piece of speech.
  - AudioChunk: pointer to raw audio in the object store plus its STT outcome.
  - RollingStateRow: one Observer update per processing window.
Used by: services/observer/service.py, services/speech/repository.py,
    services/speech/service.py, services/tests/test_speech_pipeline.py
Depends on: services/common/db.py
"""

import uuid  # UUID primary keys
from datetime import datetime  # timestamp column types
from enum import StrEnum  # string enum stored as text

from sqlalchemy import (  # column and constraint types
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
from sqlalchemy.orm import Mapped, mapped_column  # typed ORM declarations
from sqlalchemy.types import JSON  # stores the rolling-state payload

from services.common.db import Base  # declarative base


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
    # Speaker label (A, B, ...); exact because each browser uploads only its own microphone.
    candidate_label: Mapped[str] = mapped_column(String(4), index=True, nullable=False)
    # Seconds from the moment the session went LIVE, the same clock as speech_events.
    start: Mapped[float] = mapped_column(Float, nullable=False)
    end: Mapped[float] = mapped_column(Float, nullable=False)
    text: Mapped[str] = mapped_column(Text, nullable=False)
    # The STT confidence for this segment.
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

    # Where the raw audio bytes live in the object store.
    storage_key: Mapped[str] = mapped_column(String(500), nullable=False)
    # pending, transcribed or failed; failed chunks are retried (design §38).
    status: Mapped[str] = mapped_column(
        String(20), default=AudioChunkStatus.PENDING, index=True, nullable=False
    )
    # How many times speech-to-text has been tried on this chunk.
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
    # One rolling state per window per session, which stops a chunk being processed twice.
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
