"""Speech HTTP API (FR-12, FR-13, FR-15).

Audio arrives as one multipart upload per recording window, per participant.
Each browser uploads only its own microphone track, which is what makes speaker
attribution exact rather than inferred — see the note at the top of
`diarization.py`.
"""

import uuid
from typing import Annotated

from fastapi import APIRouter, File, Form, UploadFile, status
from pydantic import BaseModel, Field

from services.auth.deps import CurrentUser, SessionDep
from services.auth.models import Role
from services.common.config import settings
from services.common.contracts import TranscriptSegment
from services.common.errors import ConflictError, PermissionDeniedError
from services.realtime.hub import hub
from services.realtime.protocol import ServerEvent, server_frame
from services.session import repository as session_repo
from services.session import service as session_service
from services.session.state import SessionState
from services.speech import repository as repo
from services.speech import service
from services.speech.chunking import CHUNK_SECONDS, build_chunks, format_timestamp

router = APIRouter(prefix="/api/v1/sessions", tags=["speech"])

# A window of audio is a few hundred KB of Opus; anything far larger is a bug
# or an attempt to exhaust local disk.
MAX_AUDIO_BYTES = 25 * 1024 * 1024


class AudioAccepted(BaseModel):
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
    segments: list[SeedSegment] = Field(min_length=1, max_length=500)


class SeedResult(BaseModel):
    session_id: uuid.UUID
    segments: int
    speech_events: int


class TranscriptOut(BaseModel):
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
    gd = await session_service.require_session(db, session_id)
    participant = await session_service.require_participant(db, session_id, user)
    if SessionState(gd.state) is not SessionState.LIVE:
        raise ConflictError("audio can only be ingested while the session is live")

    payload = await audio.read()
    if not payload:
        raise ConflictError("empty audio upload")
    if len(payload) > MAX_AUDIO_BYTES:
        raise ConflictError(f"audio chunk exceeds {MAX_AUDIO_BYTES} bytes")

    before = len(await repo.list_segments(db, session_id))
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
    if gd.host_id != user.id:
        await session_service.require_participant(db, session_id, user)

    segments: list[TranscriptSegment] = await service.get_transcript(db, session_id)
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
    if settings.app_env != "local":
        raise ConflictError("seeding is only available in the local environment")

    gd = await session_service.require_session(db, session_id)
    if gd.host_id != user.id and user.role != Role.ADMIN.value:
        raise PermissionDeniedError("only the host can seed a transcript")
    if SessionState(gd.state) is not SessionState.LIVE:
        raise ConflictError("the session must be live to seed a transcript")

    known = {p.candidate_label for p in await session_repo.list_active_participants(db, gd.id)}
    for segment in payload.segments:
        if segment.candidate_label not in known:
            raise ConflictError(
                f"{segment.candidate_label!r} is not a participant "
                f"(known: {', '.join(sorted(known))})"
            )
        if segment.end < segment.start:
            raise ConflictError("a segment cannot end before it starts")

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
    if gd.host_id != user.id and user.role != Role.ADMIN.value:
        raise PermissionDeniedError("only the host can reprocess audio")
    recovered = await service.reprocess_failed(db)
    return {"recovered": recovered}
