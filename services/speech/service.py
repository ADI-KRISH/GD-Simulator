"""Audio ingestion → STT → transcript (FR-12 – FR-16, design §11, §13).

The ordering in `ingest_chunk` is the important part and is not an accident:

1. Store the raw audio **first**. If anything after this fails, the bytes are
   still on disk and the chunk can be reprocessed (design §38).
2. Transcribe.
3. Persist each segment **immediately**, one row at a time — never buffered
   until the session ends (claude.md invariant 2).
4. Publish `transcript.created` so metrics and the Observer can react without
   this module knowing they exist (design §36, §37).

An STT failure marks the chunk FAILED and returns. It never raises into the
request path and never touches session state: the live GD is not allowed to
notice that transcription is broken (claude.md invariant 1).
"""

import logging
import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from services.common.contracts import TranscriptSegment, validate_contract
from services.common.events import Event, publish
from services.common.storage import audio_key, get_object_store
from services.session.models import GDSession
from services.speech import repository as repo
from services.speech.chunking import chunk_index_for
from services.speech.diarization import map_to_session_clock
from services.speech.models import AudioChunk, AudioChunkStatus, TranscriptSegmentRow
from services.speech.stt import get_stt_provider

logger = logging.getLogger(__name__)

# Minimum text worth storing. Whisper emits stray fragments on silence, and a
# one-character segment is noise that would distort turn counts.
MIN_SEGMENT_CHARS = 2


def to_contract(row: TranscriptSegmentRow) -> TranscriptSegment:
    """Row → the canonical contract shape everything downstream consumes."""
    return TranscriptSegment(
        session_id=str(row.session_id),
        candidate_id=row.candidate_label,
        start=row.start,
        end=row.end,
        text=row.text,
        confidence=row.confidence,
    )


async def persist_segment(
    session: AsyncSession,
    session_id: uuid.UUID,
    candidate_label: str,
    start: float,
    end: float,
    text: str,
    confidence: float,
) -> TranscriptSegmentRow:
    """Write one segment and announce it. Validated against the contract before
    it is stored, so nothing malformed ever enters the source of truth."""
    segment = TranscriptSegment(
        session_id=str(session_id),
        candidate_id=candidate_label,
        start=start,
        end=max(end, start),
        text=text.strip(),
        confidence=confidence,
    )
    validate_contract(segment)

    row = TranscriptSegmentRow(
        session_id=session_id,
        candidate_label=candidate_label,
        start=segment.start,
        end=segment.end,
        text=segment.text,
        confidence=segment.confidence,
        chunk_index=chunk_index_for(segment.start),
    )
    await repo.add_segment(session, row)
    await publish(
        Event.TRANSCRIPT_CREATED,
        str(session_id),
        {
            "candidate_label": candidate_label,
            "start": segment.start,
            "end": segment.end,
            "chunk_index": row.chunk_index,
        },
    )
    return row


async def ingest_chunk(
    session: AsyncSession,
    gd: GDSession,
    candidate_label: str,
    sequence: int,
    audio: bytes,
    chunk_start: float,
    chunk_end: float,
    *,
    content_type: str = "audio/webm",
) -> AudioChunk:
    """FR-12 + FR-13. Take one uploaded audio window through the pipeline.

    Idempotent on `(session, candidate, sequence)`: a browser retrying an upload
    over a flaky connection must not produce the transcript twice.
    """
    existing = await repo.get_audio_chunk(session, gd.id, candidate_label, sequence)
    if existing is not None and existing.status == str(AudioChunkStatus.TRANSCRIBED):
        return existing

    key = audio_key(str(gd.id), candidate_label, sequence)
    store = get_object_store()
    await store.put(key, audio, content_type=content_type)

    chunk = existing or AudioChunk(
        session_id=gd.id,
        candidate_label=candidate_label,
        sequence=sequence,
        start=chunk_start,
        end=chunk_end,
        storage_key=key,
    )
    chunk.status = str(AudioChunkStatus.PENDING)
    if existing is None:
        await repo.add_audio_chunk(session, chunk)

    await transcribe_chunk(session, chunk, audio)
    return chunk


async def transcribe_chunk(
    session: AsyncSession, chunk: AudioChunk, audio: bytes | None = None
) -> AudioChunk:
    """Run STT over one stored chunk and persist whatever it produces.

    Also the reprocessing entry point: called with `audio=None` it re-reads the
    bytes from the object store, which is what makes a FAILED chunk retryable
    long after the uploader disconnected.
    """
    if audio is None:
        audio = await get_object_store().get(chunk.storage_key)

    chunk.attempts += 1
    provider = get_stt_provider()
    try:
        transcription = await provider.transcribe(audio)
    except Exception as exc:
        # Design §38: retry, keep the raw audio, reprocess the segment. The
        # live session is untouched.
        chunk.status = str(AudioChunkStatus.FAILED)
        chunk.error = str(exc)[:1000]
        await session.flush()
        logger.warning(
            "stt failed",
            extra={
                "session_id": str(chunk.session_id),
                "candidate_label": chunk.candidate_label,
                "sequence": chunk.sequence,
                "attempts": chunk.attempts,
                "error": str(exc),
            },
        )
        return chunk

    spans = map_to_session_clock(
        chunk.candidate_label,
        [(u.start, u.end) for u in transcription.utterances],
        chunk.start,
    )
    for span, utterance in zip(spans, transcription.utterances, strict=True):
        if len(utterance.text.strip()) < MIN_SEGMENT_CHARS:
            continue
        await persist_segment(
            session,
            chunk.session_id,
            chunk.candidate_label,
            span.start,
            # Never let a segment run past the window it was recorded in.
            min(span.end, chunk.end),
            utterance.text,
            utterance.confidence,
        )

    chunk.status = str(AudioChunkStatus.TRANSCRIBED)
    chunk.error = None
    await session.flush()
    return chunk


async def reprocess_failed(session: AsyncSession, limit: int = 50) -> int:
    """Retry every failed chunk (design §38, NFR-04).

    Exposed as an admin endpoint and callable from a worker; both are fine
    because `transcribe_chunk` is idempotent per chunk.
    """
    failed = await repo.list_failed_audio_chunks(session, limit)
    recovered = 0
    for chunk in failed:
        result = await transcribe_chunk(session, chunk)
        if result.status == str(AudioChunkStatus.TRANSCRIBED):
            recovered += 1
    return recovered


async def get_transcript(session: AsyncSession, session_id: uuid.UUID) -> list[TranscriptSegment]:
    """FR-15. The complete raw transcript in contract form."""
    rows = await repo.list_segments(session, session_id)
    return [to_contract(row) for row in rows]
