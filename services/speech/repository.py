"""Data access for transcripts, audio chunks and rolling states.

Purpose: Database queries for transcript segments, audio chunks and rolling states.
Why needed: Keeps SQL out of the speech and Observer services and gives them named operations
    such as latest_rolling_state or list_failed_audio_chunks.
Contains:
  - add_segment / list_segments / list_segments_for_chunk: write and read the raw transcript.
  - get_audio_chunk / add_audio_chunk / list_failed_audio_chunks: track uploads and find ones to
    retry.
  - add_rolling_state / latest_rolling_state / list_rolling_states: store and read the
    Observer's history.
  - processed_chunk_indices: which windows the Observer has already folded in.
Used by: nothing imports it directly (entry point or loaded by the framework)
Depends on: services/speech/models.py
"""

import uuid  # session id type

from sqlalchemy import select  # build queries
from sqlalchemy.ext.asyncio import AsyncSession  # database session

from services.speech.models import (  # tables and the chunk status enum
    AudioChunk,
    AudioChunkStatus,
    RollingStateRow,
    TranscriptSegmentRow,
)


async def add_segment(session: AsyncSession, row: TranscriptSegmentRow) -> TranscriptSegmentRow:
    """Insert one transcript segment immediately (never buffered)."""
    # Flush straight away rather than buffering segments until later (invariant 2).
    session.add(row)
    await session.flush()
    return row


async def list_segments(session: AsyncSession, session_id: uuid.UUID) -> list[TranscriptSegmentRow]:
    """The full raw transcript, in order. Never summarised away (design §48)."""
    # Time order, ties broken by insertion time, for a stable transcript.
    result = await session.execute(
        select(TranscriptSegmentRow)
        .where(TranscriptSegmentRow.session_id == session_id)
        .order_by(TranscriptSegmentRow.start, TranscriptSegmentRow.created_at)
    )
    return list(result.scalars())


async def list_segments_for_chunk(
    session: AsyncSession, session_id: uuid.UUID, chunk_index: int
) -> list[TranscriptSegmentRow]:
    """Transcript segments that fall in one 45-second processing window."""
    # Only segments already assigned to this window.
    result = await session.execute(
        select(TranscriptSegmentRow)
        .where(
            TranscriptSegmentRow.session_id == session_id,
            TranscriptSegmentRow.chunk_index == chunk_index,
        )
        .order_by(TranscriptSegmentRow.start)
    )
    return list(result.scalars())


async def get_audio_chunk(
    session: AsyncSession, session_id: uuid.UUID, candidate_label: str, sequence: int
) -> AudioChunk | None:
    """Find an uploaded audio window by (session, speaker, sequence) — the upload's identity."""
    # The (session, speaker, sequence) triple identifies an upload and detects retries.
    result = await session.execute(
        select(AudioChunk).where(
            AudioChunk.session_id == session_id,
            AudioChunk.candidate_label == candidate_label,
            AudioChunk.sequence == sequence,
        )
    )
    return result.scalar_one_or_none()


async def add_audio_chunk(session: AsyncSession, chunk: AudioChunk) -> AudioChunk:
    """Record a newly uploaded audio window."""
    session.add(chunk)
    await session.flush()
    return chunk


async def list_audio_chunks(session: AsyncSession, session_id: uuid.UUID) -> list[AudioChunk]:
    """Every audio chunk recorded for a session."""
    result = await session.execute(select(AudioChunk).where(AudioChunk.session_id == session_id))
    return list(result.scalars())


async def list_failed_audio_chunks(session: AsyncSession, limit: int = 50) -> list[AudioChunk]:
    """Retry candidates for the STT reprocessing path (design §38)."""
    # Oldest failures first, capped by limit so a retry pass stays bounded.
    result = await session.execute(
        select(AudioChunk)
        .where(AudioChunk.status == str(AudioChunkStatus.FAILED))
        .order_by(AudioChunk.created_at)
        .limit(limit)
    )
    return list(result.scalars())


async def add_rolling_state(session: AsyncSession, row: RollingStateRow) -> RollingStateRow:
    """Store one Observer update (history is kept, never overwritten)."""
    session.add(row)
    await session.flush()
    return row


async def latest_rolling_state(
    session: AsyncSession, session_id: uuid.UUID
) -> RollingStateRow | None:
    """The most recent Observer state for a session, or None before the first update."""
    # The highest chunk_index is the most recent state.
    result = await session.execute(
        select(RollingStateRow)
        .where(RollingStateRow.session_id == session_id)
        .order_by(RollingStateRow.chunk_index.desc())
        .limit(1)
    )
    return result.scalar_one_or_none()


async def list_rolling_states(
    session: AsyncSession, session_id: uuid.UUID
) -> list[RollingStateRow]:
    """Every Observer state for a session, oldest first."""
    # Oldest first, so the session's evolution reads in order.
    result = await session.execute(
        select(RollingStateRow)
        .where(RollingStateRow.session_id == session_id)
        .order_by(RollingStateRow.chunk_index)
    )
    return list(result.scalars())


async def processed_chunk_indices(session: AsyncSession, session_id: uuid.UUID) -> set[int]:
    """Which windows the Observer has already folded in — the loop must not
    process a chunk twice (design §15)."""
    # Just the chunk indices; the Observer subtracts these from the closed windows.
    result = await session.execute(
        select(RollingStateRow.chunk_index).where(RollingStateRow.session_id == session_id)
    )
    return set(result.scalars())
