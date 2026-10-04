"""Speech pipeline: chunking, ingestion, transcript storage, STT failure recovery.

Design §11–§16, §38. The invariant under test throughout is claude.md #2: a
transcript segment is written the moment it exists and survives everything.
"""

import uuid

import pytest
from httpx import AsyncClient

from services.common.contracts import TranscriptSegment
from services.common.storage import audio_key, get_object_store
from services.speech import repository as speech_repo
from services.speech import service as speech_service
from services.speech.chunking import (
    CHUNK_SECONDS,
    build_chunk,
    build_chunks,
    chunk_index_for,
    closed_chunk_indices,
    format_timestamp,
    parse_timestamp,
)
from services.speech.models import AudioChunkStatus
from services.speech.stt import StubSTT, set_stt_provider


def seg(label: str, start: float, text: str = "a point") -> TranscriptSegment:
    return TranscriptSegment(
        session_id="GD1", candidate_id=label, start=start, end=start + 4, text=text, confidence=0.9
    )


# --------------------------------------------------------------------------
# Chunking (design §14.2)
# --------------------------------------------------------------------------


def test_chunk_index_maps_seconds_to_windows():
    assert chunk_index_for(0) == 0
    assert chunk_index_for(CHUNK_SECONDS - 0.1) == 0
    assert chunk_index_for(CHUNK_SECONDS) == 1
    assert chunk_index_for(CHUNK_SECONDS * 2.5) == 2


def test_a_segment_is_assigned_by_its_start_time():
    """An utterance crossing a boundary stays whole in the window it began in."""
    crossing = seg("A", CHUNK_SECONDS - 2)
    chunk = build_chunk([crossing], 0)
    assert chunk.segments == [crossing]
    assert build_chunk([crossing], 1).is_empty


def test_chunks_contain_only_newly_available_speech():
    """Design §14.2: each window holds new conversation, not a running total."""
    segments = [seg("A", 5), seg("B", 20), seg("A", CHUNK_SECONDS + 5)]
    chunks = build_chunks(segments)
    assert len(chunks) == 2
    assert len(chunks[0].segments) == 2
    assert len(chunks[1].segments) == 1


def test_empty_windows_are_dropped():
    segments = [seg("A", 1), seg("A", CHUNK_SECONDS * 3 + 1)]
    indices = [c.index for c in build_chunks(segments)]
    assert indices == [0, 3]


def test_the_still_filling_window_is_never_processed():
    """Handing the Observer a half-collected window would make its update
    depend on when the loop happened to run."""
    assert closed_chunk_indices(CHUNK_SECONDS - 1) == []
    assert closed_chunk_indices(CHUNK_SECONDS) == [0]
    assert closed_chunk_indices(CHUNK_SECONDS * 3 + 10) == [0, 1, 2]


def test_chunk_dialogue_is_rendered_with_evidence_timestamps():
    chunk = build_chunk([seg("A", 65, "the cost lands elsewhere")], 1)
    assert chunk.as_dialogue() == "01:05 A: the cost lands elsewhere"


def test_timestamp_round_trip():
    assert format_timestamp(0) == "00:00"
    assert format_timestamp(314) == "05:14"
    assert parse_timestamp("05:14") == 314.0
    assert parse_timestamp("1:00:00") == 3600.0


def test_chunk_speakers_preserve_first_appearance_order():
    chunk = build_chunk([seg("B", 1), seg("A", 5), seg("B", 9)], 0)
    assert chunk.speakers == ["B", "A"]


# --------------------------------------------------------------------------
# Ingestion and storage
# --------------------------------------------------------------------------


@pytest.fixture
async def live_session_id(
    client: AsyncClient, gd_session, host_auth, candidate_auth, second_candidate
):
    session_id = gd_session["id"]
    for header in (candidate_auth, second_candidate):
        await client.post("/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=header)
        await client.post(
            f"/api/v1/lobbies/{session_id}/ready", json={"ready": True}, headers=header
        )
    await client.post(f"/api/v1/sessions/{session_id}/start", headers=host_auth)
    return session_id


async def test_uploading_audio_produces_transcript_segments(
    client: AsyncClient, live_session_id, candidate_auth
):
    """FR-12 + FR-13 + FR-15."""
    response = await client.post(
        f"/api/v1/sessions/{live_session_id}/audio",
        data={"sequence": "0", "start": "0", "end": "30"},
        files={"audio": ("chunk.webm", b"fake-opus-bytes", "audio/webm")},
        headers=candidate_auth,
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == AudioChunkStatus.TRANSCRIBED
    assert body["segments_created"] >= 1
    assert body["candidate_label"] == "A"


async def test_the_raw_audio_is_kept_for_reprocessing(
    client: AsyncClient, live_session_id, candidate_auth
):
    """Design §38: STT failure must be retryable against the original bytes."""
    await client.post(
        f"/api/v1/sessions/{live_session_id}/audio",
        data={"sequence": "0", "start": "0", "end": "30"},
        files={"audio": ("chunk.webm", b"fake-opus-bytes", "audio/webm")},
        headers=candidate_auth,
    )
    stored = await get_object_store().exists(audio_key(live_session_id, "A", 0))
    assert stored is True


async def test_a_retried_upload_does_not_transcribe_twice(
    client: AsyncClient, live_session_id, candidate_auth
):
    """A flaky network retrying the same sequence is idempotent."""
    payload = {
        "data": {"sequence": "3", "start": "90", "end": "120"},
        "files": {"audio": ("chunk.webm", b"same-bytes", "audio/webm")},
        "headers": candidate_auth,
    }
    first = await client.post(f"/api/v1/sessions/{live_session_id}/audio", **payload)
    second = await client.post(f"/api/v1/sessions/{live_session_id}/audio", **payload)

    assert first.json()["segments_created"] >= 1
    assert second.json()["segments_created"] == 0


async def test_segment_times_are_on_the_session_clock(
    client: AsyncClient, live_session_id, candidate_auth
):
    """STT reports chunk-relative times; storage must be session-relative."""
    await client.post(
        f"/api/v1/sessions/{live_session_id}/audio",
        data={"sequence": "4", "start": "120", "end": "150"},
        files={"audio": ("chunk.webm", b"later-chunk", "audio/webm")},
        headers=candidate_auth,
    )
    transcript = await client.get(
        f"/api/v1/sessions/{live_session_id}/transcript", headers=candidate_auth
    )
    lines = transcript.json()["lines"]
    assert lines
    assert all(120 <= line["start"] <= 150 for line in lines)


async def test_audio_is_refused_when_the_session_is_not_live(
    client: AsyncClient, gd_session, candidate_auth
):
    await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
    )
    response = await client.post(
        f"/api/v1/sessions/{gd_session['id']}/audio",
        data={"sequence": "0", "start": "0", "end": "30"},
        files={"audio": ("chunk.webm", b"bytes", "audio/webm")},
        headers=candidate_auth,
    )
    assert response.status_code == 409


async def test_an_empty_upload_is_rejected(client: AsyncClient, live_session_id, candidate_auth):
    response = await client.post(
        f"/api/v1/sessions/{live_session_id}/audio",
        data={"sequence": "0", "start": "0", "end": "30"},
        files={"audio": ("chunk.webm", b"", "audio/webm")},
        headers=candidate_auth,
    )
    assert response.status_code == 409


async def test_transcript_creation_is_announced_on_the_event_bus(
    client: AsyncClient, live_session_id, candidate_auth, events
):
    """Design §36: metrics and the Observer react to the event, not to a call."""
    await client.post(
        f"/api/v1/sessions/{live_session_id}/audio",
        data={"sequence": "0", "start": "0", "end": "30"},
        files={"audio": ("chunk.webm", b"bytes", "audio/webm")},
        headers=candidate_auth,
    )
    assert events.count("transcript.created") >= 1


# --------------------------------------------------------------------------
# STT failure handling (design §38)
# --------------------------------------------------------------------------


class BrokenSTT:
    name = "broken"

    async def transcribe(self, audio: bytes, *, language: str | None = None):
        raise RuntimeError("model unavailable")


async def test_stt_failure_keeps_the_audio_and_marks_the_chunk_failed(
    session_factory, client: AsyncClient, live_session_id, candidate_auth
):
    set_stt_provider(BrokenSTT())
    try:
        response = await client.post(
            f"/api/v1/sessions/{live_session_id}/audio",
            data={"sequence": "0", "start": "0", "end": "30"},
            files={"audio": ("chunk.webm", b"unreadable", "audio/webm")},
            headers=candidate_auth,
        )
    finally:
        set_stt_provider(StubSTT())

    # The upload still succeeds — a broken model must not fail the live session.
    assert response.status_code == 202
    assert response.json()["status"] == AudioChunkStatus.FAILED

    async with session_factory() as db:
        chunk = await speech_repo.get_audio_chunk(db, uuid.UUID(live_session_id), "A", 0)
        assert chunk.status == AudioChunkStatus.FAILED
        assert chunk.attempts == 1
        assert chunk.error
        # The bytes are retained so the segment can be reprocessed later.
        assert await get_object_store().exists(chunk.storage_key)


async def test_a_failed_chunk_can_be_reprocessed_once_stt_recovers(
    session_factory, client: AsyncClient, live_session_id, candidate_auth, host_auth
):
    set_stt_provider(BrokenSTT())
    try:
        await client.post(
            f"/api/v1/sessions/{live_session_id}/audio",
            data={"sequence": "0", "start": "0", "end": "30"},
            files={"audio": ("chunk.webm", b"unreadable", "audio/webm")},
            headers=candidate_auth,
        )
    finally:
        set_stt_provider(StubSTT())

    response = await client.post(
        f"/api/v1/sessions/{live_session_id}/transcript/reprocess", headers=host_auth
    )
    assert response.status_code == 200
    assert response.json()["recovered"] == 1

    async with session_factory() as db:
        segments = await speech_repo.list_segments(db, uuid.UUID(live_session_id))
        assert segments
        chunk = await speech_repo.get_audio_chunk(db, uuid.UUID(live_session_id), "A", 0)
        assert chunk.status == AudioChunkStatus.TRANSCRIBED
        assert chunk.attempts == 2


async def test_only_the_host_can_reprocess(client: AsyncClient, live_session_id, candidate_auth):
    response = await client.post(
        f"/api/v1/sessions/{live_session_id}/transcript/reprocess", headers=candidate_auth
    )
    assert response.status_code == 403


async def test_segments_are_persisted_individually_as_they_arrive(session_factory, live_session_id):
    """claude.md invariant 2: no end-of-session batch write."""
    session_id = uuid.UUID(live_session_id)
    async with session_factory() as db:
        await speech_service.persist_segment(db, session_id, "A", 0.0, 5.0, "first", 0.9)
        await db.commit()
    async with session_factory() as db:
        # Visible immediately, before anything else happens to the session.
        assert len(await speech_repo.list_segments(db, session_id)) == 1
        await speech_service.persist_segment(db, session_id, "B", 6.0, 9.0, "second", 0.9)
        await db.commit()
    async with session_factory() as db:
        assert len(await speech_repo.list_segments(db, session_id)) == 2


async def test_a_participant_cannot_upload_audio_for_someone_else(
    client: AsyncClient, live_session_id, candidate_auth, second_candidate
):
    """Attribution comes from the authenticated participant, never the payload."""
    response = await client.post(
        f"/api/v1/sessions/{live_session_id}/audio",
        data={"sequence": "0", "start": "0", "end": "30", "candidate_label": "A"},
        files={"audio": ("chunk.webm", b"bytes", "audio/webm")},
        headers=second_candidate,
    )
    assert response.status_code == 202
    # B's upload is attributed to B regardless of what the form claimed.
    assert response.json()["candidate_label"] == "B"


# --------------------------------------------------------------------------
# Transcript seeding — a local-only demo affordance
# --------------------------------------------------------------------------


SEED_BODY = {
    "segments": [
        {"candidate_label": "A", "start": 0, "end": 20, "text": "Regulation is overdue."},
        {"candidate_label": "B", "start": 21, "end": 35, "text": "I disagree with that framing."},
    ]
}


async def test_seeding_writes_transcript_and_speech_events(
    session_factory, client: AsyncClient, live_session_id, host_auth
):
    """It must write the same rows the speech pipeline would, so everything
    downstream behaves identically to a real session."""
    response = await client.post(
        f"/api/v1/sessions/{live_session_id}/transcript/seed", json=SEED_BODY, headers=host_auth
    )
    assert response.status_code == 200, response.text
    assert response.json()["segments"] == 2

    async with session_factory() as db:
        session_id = uuid.UUID(live_session_id)
        assert len(await speech_repo.list_segments(db, session_id)) == 2
        from services.session import repository as session_repo

        assert len(await session_repo.list_speech_events(db, session_id)) == 2


async def test_seeding_is_host_only(client: AsyncClient, live_session_id, candidate_auth):
    response = await client.post(
        f"/api/v1/sessions/{live_session_id}/transcript/seed",
        json=SEED_BODY,
        headers=candidate_auth,
    )
    assert response.status_code == 403


async def test_seeding_rejects_an_unknown_candidate(
    client: AsyncClient, live_session_id, host_auth
):
    """A typo'd label would otherwise create a phantom participant with a score."""
    response = await client.post(
        f"/api/v1/sessions/{live_session_id}/transcript/seed",
        json={"segments": [{"candidate_label": "Z", "start": 0, "end": 5, "text": "hello"}]},
        headers=host_auth,
    )
    assert response.status_code == 409
    assert "not a participant" in response.text


async def test_seeding_requires_a_live_session(
    client: AsyncClient, gd_session, host_auth, candidate_auth
):
    await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
    )
    response = await client.post(
        f"/api/v1/sessions/{gd_session['id']}/transcript/seed", json=SEED_BODY, headers=host_auth
    )
    assert response.status_code == 409


async def test_seeding_is_refused_outside_the_local_environment(
    client: AsyncClient, live_session_id, host_auth
):
    """Guarded twice — it must never be reachable where transcripts are evidence."""
    from services.common.config import settings

    settings.app_env = "production"
    try:
        response = await client.post(
            f"/api/v1/sessions/{live_session_id}/transcript/seed",
            json=SEED_BODY,
            headers=host_auth,
        )
        assert response.status_code == 409
        assert "local environment" in response.text
    finally:
        settings.app_env = "local"
