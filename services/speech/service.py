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

Purpose: Takes one uploaded audio window through the pipeline: store the raw audio, transcribe
    it, save each transcript segment and announce it. Also retries failed chunks and returns the
    full transcript.
Why needed: This is the speech slice's business logic. The ordering (audio first, segments saved
    one by one, STT failure contained) is what guarantees the transcript is the source of truth
    and that a broken transcriber can never disturb the live discussion (invariants 1 and 2).
Contains:
  - MIN_SEGMENT_CHARS: drop stray fragments Whisper emits on silence.
  - to_contract: turn a transcript row into the contract shape everything downstream reads.
  - persist_segment: validate, store and announce one segment immediately.
  - ingest_chunk: idempotent upload handling (FR-12, FR-13).
  - transcribe_chunk: run STT on a stored chunk; also the reprocessing entry point.
  - reprocess_failed: retry every FAILED chunk (design §38).
  - get_transcript: the complete raw transcript (FR-15).
Used by: services/evaluation/service.py, services/metrics/service.py,
    services/observer/service.py, services/speech/router.py,
    services/tests/test_evaluation_api.py, services/tests/test_evaluation_pipeline.py,
    services/tests/test_observer.py
Depends on: services/common/contracts.py, services/common/events.py, services/common/storage.py,
    services/session/models.py, services/speech/__init__.py, services/speech/chunking.py,
    services/speech/diarization.py, services/speech/models.py, services/speech/stt.py
"""

import logging  # warn on STT failure
import uuid  # session id type

from sqlalchemy.ext.asyncio import AsyncSession  # database session

from services.common.contracts import TranscriptSegment, validate_contract  # transcript contract
from services.common.events import Event, publish  # announce transcript.created
from services.common.storage import audio_key, get_object_store  # keep raw audio for retries
from services.observability.recorder import span as trace_span  # time the speech-to-text call
from services.session.models import GDSession  # session row type
from services.speech import repository as repo  # data access
from services.speech.chunking import chunk_index_for  # which 45s window a time falls in
from services.speech.diarization import map_to_session_clock  # chunk time -> session time
from services.speech.models import AudioChunk, AudioChunkStatus, TranscriptSegmentRow  # tables
from services.speech.stt import get_stt_provider  # configured speech-to-text backend

logger = logging.getLogger(__name__)

# Minimum text worth storing. Whisper emits stray fragments on silence, and a
# one-character segment is noise that would distort turn counts.
MIN_SEGMENT_CHARS = 2


def to_contract(row: TranscriptSegmentRow) -> TranscriptSegment:
    """Row → the canonical contract shape everything downstream consumes."""
    # Translate the row into the contract shape (candidate_label becomes candidate_id) used by
    # metrics, the Observer and evaluation.
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
    # Build the contract object first so bad data is caught before storage.
    segment = TranscriptSegment(
        session_id=str(session_id),
        candidate_id=candidate_label,
        start=start,
        end=max(end, start),
        text=text.strip(),
        confidence=confidence,
    )
    # Raises if the segment breaks the schema, so nothing malformed becomes source of truth.
    validate_contract(segment)

    # chunk_index is set now so the Observer can find segments by window.
    row = TranscriptSegmentRow(
        session_id=session_id,
        candidate_label=candidate_label,
        start=segment.start,
        end=segment.end,
        text=segment.text,
        confidence=segment.confidence,
        chunk_index=chunk_index_for(segment.start),
    )
    # Persist at once (invariant 2) rather than waiting for the session to end.
    await repo.add_segment(session, row)
    # Tell metrics and the Observer a new segment exists, without importing them.
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
    # The same (session, speaker, sequence) means this is a retry of an earlier upload.
    existing = await repo.get_audio_chunk(session, gd.id, candidate_label, sequence)
    # Already transcribed: return it so a retried upload does not duplicate the transcript.
    if existing is not None and existing.status == str(AudioChunkStatus.TRANSCRIBED):
        return existing

    # A deterministic storage key per chunk.
    key = audio_key(str(gd.id), candidate_label, sequence)
    store = get_object_store()
    # Step 1: keep the raw audio before anything can fail (design §38).
    await store.put(key, audio, content_type=content_type)

    # Reuse the row from a failed earlier attempt, otherwise create a new one.
    chunk = existing or AudioChunk(
        session_id=gd.id,
        candidate_label=candidate_label,
        sequence=sequence,
        start=chunk_start,
        end=chunk_end,
        storage_key=key,
    )
    # Reset to PENDING while this attempt runs.
    chunk.status = str(AudioChunkStatus.PENDING)
    if existing is None:
        await repo.add_audio_chunk(session, chunk)

    # Steps 2 and 3: transcribe, then persist the segments.
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
    # Reprocessing: fetch the stored bytes instead of needing the uploader.
    if audio is None:
        audio = await get_object_store().get(chunk.storage_key)

    # Count every attempt for diagnostics and retry limits.
    chunk.attempts += 1
    # Whichever backend is configured (Whisper, ElevenLabs, OpenAI or the stub).
    provider = get_stt_provider()
    # Any STT failure is contained here: mark the chunk FAILED and return.
    try:
        async with trace_span(
            "stt", provider.name, purpose="speech", session_id=str(chunk.session_id),
            provider=provider.name, meta={"audio_bytes": len(audio), "attempt": chunk.attempts},
        ):  # fmt: skip
            transcription = await provider.transcribe(audio)
    except Exception as exc:
        # Design §38: retry, keep the raw audio, reprocess the segment. The
        # live session is untouched.
        chunk.status = str(AudioChunkStatus.FAILED)
        # Keep the error text (truncated) for diagnosis.
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

    # STT times are relative to the chunk; shift them onto the session clock.
    spans = map_to_session_clock(
        chunk.candidate_label,
        [(u.start, u.end) for u in transcription.utterances],
        chunk.start,
    )
    # strict=True guarantees spans and utterances line up one to one.
    for span, utterance in zip(spans, transcription.utterances, strict=True):
        # Drop stray fragments that Whisper emits on silence.
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

    # All segments stored: mark the chunk done and clear any earlier error.
    chunk.status = str(AudioChunkStatus.TRANSCRIBED)
    chunk.error = None
    await session.flush()
    return chunk


async def reprocess_failed(session: AsyncSession, limit: int = 50) -> int:
    """Retry every failed chunk (design §38, NFR-04).

    Exposed as an admin endpoint and callable from a worker; both are fine
    because `transcribe_chunk` is idempotent per chunk.
    """
    # Oldest failed chunks first, bounded by limit.
    failed = await repo.list_failed_audio_chunks(session, limit)
    recovered = 0
    # Each retry is independent; one failure does not stop the others.
    for chunk in failed:
        result = await transcribe_chunk(session, chunk)
        if result.status == str(AudioChunkStatus.TRANSCRIBED):
            recovered += 1
    return recovered


async def get_transcript(session: AsyncSession, session_id: uuid.UUID) -> list[TranscriptSegment]:
    """FR-15. The complete raw transcript in contract form."""
    # Rows come back in time order (see repository.list_segments).
    rows = await repo.list_segments(session, session_id)
    return [to_contract(row) for row in rows]
