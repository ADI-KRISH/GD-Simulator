"""Speech HTTP API (FR-12, FR-13, FR-15).

Audio arrives as one multipart upload per recording window, per participant.
Each browser uploads only its own microphone track, which is what makes speaker
attribution exact rather than inferred — see the note at the top of
`diarization.py`.

Purpose: HTTP endpoints for speech: upload one audio window, read the transcript, seed a
    scripted transcript for demos, and retry failed transcriptions.
Why needed: It is the front door of the speech pipeline. Each browser uploads only its own
    microphone through here, which is what makes speaker attribution exact, and the transcript
    endpoint serves the raw source of truth to the UI.
Contains:
  - MAX_AUDIO_BYTES: upload size cap.
  - AudioAccepted / TranscriptLine / TranscriptOut: response shapes.
  - SeedSegment / SeedRequest / SeedResult: the demo seeding bodies.
  - upload_audio: FR-12/13, accept one recording window and run it through service.ingest_chunk.
  - get_transcript: FR-15, the full raw transcript with display timestamps.
  - seed_transcript: local-only, host-only demo tool that writes the rows the real pipeline
    would.
  - reprocess_failed_audio: design §38, retry chunks whose transcription failed.
Used by: services/main.py
Depends on: services/auth/deps.py, services/auth/models.py, services/common/config.py,
    services/common/contracts.py, services/common/errors.py, services/realtime/hub.py,
    services/realtime/protocol.py, services/session/__init__.py, services/session/state.py,
    services/speech/__init__.py, services/speech/chunking.py, services/speech/service.py
"""

import uuid  # session id path parameter
from typing import Annotated  # attach Form/File metadata to parameters

from fastapi import APIRouter, File, Form, UploadFile, status  # routing and multipart upload
from pydantic import BaseModel, Field  # request/response models

from services.auth.deps import CurrentUser, SessionDep  # injected user and DB session
from services.auth.models import Role  # admin check for host-only tools
from services.common.config import settings  # app_env gate for the seed endpoint
from services.common.contracts import TranscriptSegment  # transcript contract type
from services.common.errors import ConflictError, PermissionDeniedError  # 409 / 403
from services.realtime.hub import hub  # tell the room the transcript grew
from services.realtime.protocol import ServerEvent, server_frame  # WebSocket event frames
from services.session import repository as session_repo  # participant lookups
from services.session import service as session_service  # access checks, speech events
from services.session.state import SessionState  # require LIVE for ingestion
from services.speech import repository as repo  # transcript data access
from services.speech import service  # ingestion pipeline
from services.speech.chunking import CHUNK_SECONDS, build_chunks, format_timestamp  # windows

router = APIRouter(prefix="/api/v1/sessions", tags=["speech"])

# A window of audio is a few hundred KB of Opus; anything far larger is a bug
# or an attempt to exhaust local disk.
MAX_AUDIO_BYTES = 25 * 1024 * 1024


class AudioAccepted(BaseModel):
    """Response to an audio upload: what was received and how many segments it produced."""

    session_id: uuid.UUID
    candidate_label: str
    sequence: int
    status: str
    segments_created: int


class TranscriptLine(BaseModel):
    """Transcript rendered for display (FR-15)."""

    timestamp: str
    candidate_label: str
    text: str
    start: float
    end: float
    confidence: float


class SeedSegment(BaseModel):
    """One scripted line of dialogue."""

    candidate_label: str = Field(min_length=1, max_length=4)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str = Field(min_length=1)
    confidence: float = Field(default=0.95, ge=0, le=1)


class SeedRequest(BaseModel):
    """Body of the demo seed endpoint: a scripted transcript."""

    segments: list[SeedSegment] = Field(min_length=1, max_length=500)


class SeedResult(BaseModel):
    """How many segments and speech events the seed created."""

    session_id: uuid.UUID
    segments: int
    speech_events: int


class TranscriptOut(BaseModel):
    """The full transcript plus counts, as returned by GET /transcript."""

    session_id: uuid.UUID
    segment_count: int
    chunk_seconds: float = Field(default=CHUNK_SECONDS)
    chunk_count: int
    lines: list[TranscriptLine]


@router.post(
    "/{session_id}/audio", response_model=AudioAccepted, status_code=status.HTTP_202_ACCEPTED
)
async def upload_audio(
    session_id: uuid.UUID,
    db: SessionDep,
    user: CurrentUser,
    sequence: Annotated[int, Form(ge=0)],
    start: Annotated[float, Form(ge=0)],
    end: Annotated[float, Form(ge=0)],
    audio: Annotated[UploadFile, File()],
) -> AudioAccepted:
    """FR-12 + FR-13. One recording window from one participant's microphone."""
    # 404 if the session does not exist.
    gd = await session_service.require_session(db, session_id)
    # Only seated participants can upload; the speaker label comes from their seat, never from the
    # request, so nobody can speak as someone else.
    participant = await session_service.require_participant(db, session_id, user)
    # Audio only counts during the live discussion.
    if SessionState(gd.state) is not SessionState.LIVE:
        raise ConflictError("audio can only be ingested while the session is live")

    # Read the whole upload so its size can be checked before storing it.
    payload = await audio.read()
    if not payload:
        raise ConflictError("empty audio upload")
    # Reject oversized uploads to protect local disk.
    if len(payload) > MAX_AUDIO_BYTES:
        raise ConflictError(f"audio chunk exceeds {MAX_AUDIO_BYTES} bytes")

    # Count segments before and after so the response can report how many this upload created.
    before = len(await repo.list_segments(db, session_id))
    # Store the audio, transcribe it and persist the segments (idempotent per sequence number).
    chunk = await service.ingest_chunk(
        db,
        gd,
        participant.candidate_label,
        sequence,
        payload,
        chunk_start=start,
        chunk_end=max(end, start),
        content_type=audio.content_type or "audio/webm",
    )
    after = len(await repo.list_segments(db, session_id))

    # Only announce when something new was transcribed.
    if after > before:
        # Commit first: clients refetch the transcript as soon as they hear this.
        await db.commit()
        await hub.broadcast(
            str(gd.id),
            server_frame(
                ServerEvent.TRANSCRIPT_APPENDED,
                str(gd.id),
                {"candidate_label": participant.candidate_label, "segments": after - before},
            ),
        )

    return AudioAccepted(
        session_id=session_id,
        candidate_label=participant.candidate_label,
        sequence=sequence,
        status=chunk.status,
        segments_created=after - before,
    )


@router.get("/{session_id}/transcript", response_model=TranscriptOut)
async def get_transcript(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> TranscriptOut:
    """FR-15. The raw transcript — the source of truth, not a summary."""
    gd = await session_service.require_session(db, session_id)
    # The host or a participant may read the transcript.
    if gd.host_id != user.id:
        await session_service.require_participant(db, session_id, user)

    # The raw transcript, never a summary.
    segments: list[TranscriptSegment] = await service.get_transcript(db, session_id)
    # Add display fields: mm:ss timestamps and the number of 45-second windows.
    return TranscriptOut(
        session_id=session_id,
        segment_count=len(segments),
        chunk_count=len(build_chunks(segments)),
        lines=[
            TranscriptLine(
                timestamp=format_timestamp(s.start),
                candidate_label=s.candidate_id,
                text=s.text,
                start=s.start,
                end=s.end,
                confidence=s.confidence,
            )
            for s in segments
        ],
    )


@router.post("/{session_id}/transcript/seed", response_model=SeedResult)
async def seed_transcript(
    session_id: uuid.UUID, payload: SeedRequest, db: SessionDep, user: CurrentUser
) -> SeedResult:
    """Inject a scripted transcript into a live session.

    A demo and testing affordance, not part of the product flow: evaluating a
    real discussion otherwise requires several people, working microphones and
    fifteen minutes. This writes the same rows the speech pipeline would — one
    transcript segment and one speech event per line — so everything downstream
    (metrics, observer, evaluation) behaves identically to a genuine session.

    Guarded twice: host-only, and refused unless `APP_ENV=local`. It must never
    be reachable anywhere transcripts are evidence.
    """
    # First guard: refuse outside the local environment.
    if settings.app_env != "local":
        raise ConflictError("seeding is only available in the local environment")

    gd = await session_service.require_session(db, session_id)
    # Second guard: only the host or an admin.
    if gd.host_id != user.id and user.role != Role.ADMIN.value:
        raise PermissionDeniedError("only the host can seed a transcript")
    # Seeded lines need the live clock, like real speech.
    if SessionState(gd.state) is not SessionState.LIVE:
        raise ConflictError("the session must be live to seed a transcript")

    # Labels that really belong to someone in the room.
    known = {p.candidate_label for p in await session_repo.list_active_participants(db, gd.id)}
    # Validate every line before writing any, so a bad script changes nothing.
    for segment in payload.segments:
        if segment.candidate_label not in known:
            raise ConflictError(
                f"{segment.candidate_label!r} is not a participant "
                f"(known: {', '.join(sorted(known))})"
            )
        if segment.end < segment.start:
            raise ConflictError("a segment cannot end before it starts")

    # Write exactly what the real pipeline writes: a speech event and a transcript segment per line.
    for segment in payload.segments:
        end = max(segment.end, segment.start)
        await session_service.record_speech(db, gd, segment.candidate_label, segment.start, end)
        await service.persist_segment(
            db, gd.id, segment.candidate_label, segment.start, end, segment.text,
            segment.confidence,
        )

    return SeedResult(
        session_id=session_id, segments=len(payload.segments), speech_events=len(payload.segments)
    )


@router.post("/{session_id}/transcript/reprocess", response_model=dict)
async def reprocess_failed_audio(session_id: uuid.UUID, db: SessionDep, user: CurrentUser) -> dict:
    """Design §38: retry failed STT against the retained raw audio.

    Host-only, and safe to call repeatedly — transcription is idempotent per
    chunk.
    """
    gd = await session_service.require_session(db, session_id)
    # Only the host or an admin may trigger a retry.
    if gd.host_id != user.id and user.role != Role.ADMIN.value:
        raise PermissionDeniedError("only the host can reprocess audio")
    # Retries every FAILED chunk (not only this session's), bounded by the default limit.
    recovered = await service.reprocess_failed(db)
    return {"recovered": recovered}
