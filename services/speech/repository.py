"""Data access for transcripts, audio chunks and rolling states."""

import uuid

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from services.speech.models import (
    AudioChunk,
    AudioChunkStatus,
    RollingStateRow,
    TranscriptSegmentRow,
)


async def add_segment(session: AsyncSession, row: TranscriptSegmentRow) -> TranscriptSegmentRow:
    session.add(row)
    await session.flush()
    return row


async def list_segments(session: AsyncSession, session_id: uuid.UUID) -> list[TranscriptSegmentRow]:
    """The full raw transcript, in order. Never summarised away (design §48)."""
    result = await session.execute(
        select(TranscriptSegmentRow)
        .where(TranscriptSegmentRow.session_id == session_id)
        .order_by(TranscriptSegmentRow.start, TranscriptSegmentRow.created_at)
    )
    return list(result.scalars())


async def list_segments_for_chunk(
    session: AsyncSession, session_id: uuid.UUID, chunk_index: int
) -> list[TranscriptSegmentRow]:
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
    result = await session.execute(
        select(AudioChunk).where(
            AudioChunk.session_id == session_id,
            AudioChunk.candidate_label == candidate_label,
            AudioChunk.sequence == sequence,
        )
    )
    return result.scalar_one_or_none()


async def add_audio_chunk(session: AsyncSession, chunk: AudioChunk) -> AudioChunk:
    session.add(chunk)
    await session.flush()
    return chunk


async def list_failed_audio_chunks(session: AsyncSession, limit: int = 50) -> list[AudioChunk]:
    """Retry candidates for the STT reprocessing path (design §38)."""
    result = await session.execute(
        select(AudioChunk)
        .where(AudioChunk.status == str(AudioChunkStatus.FAILED))
        .order_by(AudioChunk.created_at)
        .limit(limit)
    )
    return list(result.scalars())


async def add_rolling_state(session: AsyncSession, row: RollingStateRow) -> RollingStateRow:
    session.add(row)
    await session.flush()
    return row


async def latest_rolling_state(
    session: AsyncSession, session_id: uuid.UUID
) -> RollingStateRow | None:
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
    result = await session.execute(
        select(RollingStateRow)
        .where(RollingStateRow.session_id == session_id)
        .order_by(RollingStateRow.chunk_index)
    )
    return list(result.scalars())


async def processed_chunk_indices(session: AsyncSession, session_id: uuid.UUID) -> set[int]:
    """Which windows the Observer has already folded in — the loop must not
    process a chunk twice (design §15)."""
    result = await session.execute(
        select(RollingStateRow.chunk_index).where(RollingStateRow.session_id == session_id)
    )
    return set(result.scalars())
