"""Speech pipeline: chunking, ingestion, transcript storage, STT failure recovery.

Design §11–§16, §38. The invariant under test throughout is claude.md #2: a
transcript segment is written the moment it exists and survives everything.

Purpose: Tests for the speech pipeline: chunk windows and timestamps, audio upload and
    transcription, idempotent retries, session-clock timing, STT failure and recovery,
    per-segment persistence and the local-only transcript seeding tool.
Why needed: The invariant under test throughout is that a transcript segment is written the
    moment it exists and survives everything (invariant 2). These tests also prove a broken
    transcriber never breaks the live session and that speaker labels cannot be forged.
Contains:
  - seg: helper that builds a 4-second transcript segment.
  - Chunking tests: window indices, assignment by start time, empty windows dropped,
    still-filling window never processed, dialogue rendering, timestamp round trip, speaker
    order.
  - live_session_id: fixture with a LIVE two-candidate session.
  - Upload tests: segments produced, raw audio kept, retries idempotent, times on the session
    clock, refused when not live or empty, event published.
  - BrokenSTT and failure tests: failed chunk keeps its audio and can be reprocessed by the host
    only.
  - Persistence and attribution tests: segments saved one by one, a participant cannot upload as
    someone else.
  - Seeding tests: writes transcript and speech events, host-only, rejects unknown labels, needs
    a live session, refused outside local.
Used by: nothing imports it directly (run by pytest, which discovers test_*.py files)
Depends on: services/common/contracts.py, services/common/storage.py,
    services/speech/__init__.py, services/speech/chunking.py, services/speech/models.py,
    services/speech/stt.py
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
    """Build a 4-second transcript segment for a candidate."""
    return TranscriptSegment(
        session_id="GD1", candidate_id=label, start=start, end=start + 4, text=text, confidence=0.9
    )


# --------------------------------------------------------------------------
# Chunking (design §14.2)
# --------------------------------------------------------------------------


def test_chunk_index_maps_seconds_to_windows():
    """Seconds map to 45-second window indices."""
    # Boundaries: just before 45 seconds is window 0, exactly 45 seconds is window 1.
    assert chunk_index_for(0) == 0
    assert chunk_index_for(CHUNK_SECONDS - 0.1) == 0
    assert chunk_index_for(CHUNK_SECONDS) == 1
    assert chunk_index_for(CHUNK_SECONDS * 2.5) == 2


def test_a_segment_is_assigned_by_its_start_time():
    """An utterance crossing a boundary stays whole in the window it began in."""
    # A 4-second segment that starts 2 seconds before the boundary.
    crossing = seg("A", CHUNK_SECONDS - 2)
    chunk = build_chunk([crossing], 0)
    # It belongs to window 0 only, because windows are chosen by start time.
    assert chunk.segments == [crossing]
    assert build_chunk([crossing], 1).is_empty


def test_chunks_contain_only_newly_available_speech():
    """Design §14.2: each window holds new conversation, not a running total."""
    # Two segments in window 0 and one in window 1.
    segments = [seg("A", 5), seg("B", 20), seg("A", CHUNK_SECONDS + 5)]
    chunks = build_chunks(segments)
    assert len(chunks) == 2
    assert len(chunks[0].segments) == 2
    assert len(chunks[1].segments) == 1


def test_empty_windows_are_dropped():
    """build_chunks skips windows with no speech."""
    # Speech in windows 0 and 3 only.
    segments = [seg("A", 1), seg("A", CHUNK_SECONDS * 3 + 1)]
    indices = [c.index for c in build_chunks(segments)]
    # The silent windows 1 and 2 are dropped.
    assert indices == [0, 3]


def test_the_still_filling_window_is_never_processed():
    """Handing the Observer a half-collected window would make its update
    depend on when the loop happened to run."""
    # Window 0 only closes once its 45 seconds have elapsed.
    assert closed_chunk_indices(CHUNK_SECONDS - 1) == []
    assert closed_chunk_indices(CHUNK_SECONDS) == [0]
    assert closed_chunk_indices(CHUNK_SECONDS * 3 + 10) == [0, 1, 2]


def test_chunk_dialogue_is_rendered_with_evidence_timestamps():
    """Dialogue lines carry mm:ss timestamps."""
    # A segment at 65 seconds belongs in window 1.
    chunk = build_chunk([seg("A", 65, "the cost lands elsewhere")], 1)
    # Rendered as mm:ss, speaker label and text.
    assert chunk.as_dialogue() == "01:05 A: the cost lands elsewhere"


def test_timestamp_round_trip():
    """format_timestamp and parse_timestamp are inverses."""
    # Formatting and parsing round-trip, including the hh:mm:ss form.
    assert format_timestamp(0) == "00:00"
    assert format_timestamp(314) == "05:14"
    assert parse_timestamp("05:14") == 314.0
    assert parse_timestamp("1:00:00") == 3600.0


def test_chunk_speakers_preserve_first_appearance_order():
    """A chunk lists speakers in order of first appearance."""
    # B speaks first, then A, then B again.
    chunk = build_chunk([seg("B", 1), seg("A", 5), seg("B", 9)], 0)
    # Order of first appearance, with no duplicates.
    assert chunk.speakers == ["B", "A"]


# --------------------------------------------------------------------------
# Ingestion and storage
# --------------------------------------------------------------------------


@pytest.fixture
async def live_session_id(
    client: AsyncClient, gd_session, host_auth, candidate_auth, second_candidate
):
    """A LIVE two-candidate session ready to receive audio."""
    session_id = gd_session["id"]
    # Join, ready up and start, so the session is LIVE.
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
    # Upload one 30-second window as candidate A, with fake audio bytes.
    response = await client.post(
        f"/api/v1/sessions/{live_session_id}/audio",
        data={"sequence": "0", "start": "0", "end": "30"},
        files={"audio": ("chunk.webm", b"fake-opus-bytes", "audio/webm")},
        headers=candidate_auth,
    )
    # Accepted and transcribed by the stub STT, with at least one segment attributed to A.
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == AudioChunkStatus.TRANSCRIBED
    assert body["segments_created"] >= 1
    assert body["candidate_label"] == "A"


async def test_the_raw_audio_is_kept_for_reprocessing(
    client: AsyncClient, live_session_id, candidate_auth
):
    """Design §38: STT failure must be retryable against the original bytes."""
    # Upload a chunk.
    await client.post(
        f"/api/v1/sessions/{live_session_id}/audio",
        data={"sequence": "0", "start": "0", "end": "30"},
        files={"audio": ("chunk.webm", b"fake-opus-bytes", "audio/webm")},
        headers=candidate_auth,
    )
    # The raw bytes exist in the object store under the deterministic key.
    stored = await get_object_store().exists(audio_key(live_session_id, "A", 0))
    assert stored is True


async def test_a_retried_upload_does_not_transcribe_twice(
    client: AsyncClient, live_session_id, candidate_auth
):
    """A flaky network retrying the same sequence is idempotent."""
    # Identical request arguments, reused for both uploads.
    payload = {
        "data": {"sequence": "3", "start": "90", "end": "120"},
        "files": {"audio": ("chunk.webm", b"same-bytes", "audio/webm")},
        "headers": candidate_auth,
    }
    # The first upload transcribes.
    first = await client.post(f"/api/v1/sessions/{live_session_id}/audio", **payload)
    # The same sequence again, as a browser retry would send.
    second = await client.post(f"/api/v1/sessions/{live_session_id}/audio", **payload)

    assert first.json()["segments_created"] >= 1
    # The retry creates nothing new.
    assert second.json()["segments_created"] == 0


async def test_segment_times_are_on_the_session_clock(
    client: AsyncClient, live_session_id, candidate_auth
):
    """STT reports chunk-relative times; storage must be session-relative."""
    # A chunk covering 120 to 150 seconds of the discussion.
    await client.post(
        f"/api/v1/sessions/{live_session_id}/audio",
        data={"sequence": "4", "start": "120", "end": "150"},
        files={"audio": ("chunk.webm", b"later-chunk", "audio/webm")},
        headers=candidate_auth,
    )
    # Read the transcript back.
    transcript = await client.get(
        f"/api/v1/sessions/{live_session_id}/transcript", headers=candidate_auth
    )
    lines = transcript.json()["lines"]
    assert lines
    # Every line falls inside that window on the session clock.
    assert all(120 <= line["start"] <= 150 for line in lines)


async def test_audio_is_refused_when_the_session_is_not_live(
    client: AsyncClient, gd_session, candidate_auth
):
    """Audio upload before the session is live is refused."""
    # Join the lobby only; the session never starts.
    await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
    )
    # Try to upload audio before the session is live.
    response = await client.post(
        f"/api/v1/sessions/{gd_session['id']}/audio",
        data={"sequence": "0", "start": "0", "end": "30"},
        files={"audio": ("chunk.webm", b"bytes", "audio/webm")},
        headers=candidate_auth,
    )
    assert response.status_code == 409


async def test_an_empty_upload_is_rejected(client: AsyncClient, live_session_id, candidate_auth):
    """An empty audio file is rejected with 409."""
    # A zero-byte audio file.
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
    # A normal upload.
    await client.post(
        f"/api/v1/sessions/{live_session_id}/audio",
        data={"sequence": "0", "start": "0", "end": "30"},
        files={"audio": ("chunk.webm", b"bytes", "audio/webm")},
        headers=candidate_auth,
    )
    # At least one transcript.created event was published.
    assert events.count("transcript.created") >= 1


# --------------------------------------------------------------------------
# STT failure handling (design §38)
# --------------------------------------------------------------------------


# A fake STT provider used to simulate an outage.
class BrokenSTT:
    """A speech-to-text provider that always fails."""
    name = "broken"

    async def transcribe(self, audio: bytes, *, language: str | None = None):
        """Always raise, to simulate an STT outage."""
        raise RuntimeError("model unavailable")


async def test_stt_failure_keeps_the_audio_and_marks_the_chunk_failed(
    session_factory, client: AsyncClient, live_session_id, candidate_auth
):
    """When STT fails the audio is kept and the chunk is marked FAILED."""
    # Install the failing provider.
    set_stt_provider(BrokenSTT())
    # Upload while STT is down, then always restore the stub.
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

    # Inspect the chunk row and the stored audio.
    async with session_factory() as db:
        chunk = await speech_repo.get_audio_chunk(db, uuid.UUID(live_session_id), "A", 0)
        assert chunk.status == AudioChunkStatus.FAILED
        # One failed attempt was recorded, together with its error text.
        assert chunk.attempts == 1
        assert chunk.error
        # The bytes are retained so the segment can be reprocessed later.
        assert await get_object_store().exists(chunk.storage_key)


async def test_a_failed_chunk_can_be_reprocessed_once_stt_recovers(
    session_factory, client: AsyncClient, live_session_id, candidate_auth, host_auth
):
    """A failed chunk is transcribed on reprocess once STT works again."""
    # STT is down for the first upload...
    set_stt_provider(BrokenSTT())
    try:
        await client.post(
            f"/api/v1/sessions/{live_session_id}/audio",
            data={"sequence": "0", "start": "0", "end": "30"},
            files={"audio": ("chunk.webm", b"unreadable", "audio/webm")},
            headers=candidate_auth,
        )
    finally:
        # ...then it recovers.
        set_stt_provider(StubSTT())

    # The host triggers reprocessing.
    response = await client.post(
        f"/api/v1/sessions/{live_session_id}/transcript/reprocess", headers=host_auth
    )
    assert response.status_code == 200
    # The failed chunk is recovered.
    assert response.json()["recovered"] == 1

    # Verify its transcript segments now exist.
    async with session_factory() as db:
        segments = await speech_repo.list_segments(db, uuid.UUID(live_session_id))
        assert segments
        chunk = await speech_repo.get_audio_chunk(db, uuid.UUID(live_session_id), "A", 0)
        assert chunk.status == AudioChunkStatus.TRANSCRIBED
        # Two attempts in total: the failure and the retry.
        assert chunk.attempts == 2


async def test_only_the_host_can_reprocess(client: AsyncClient, live_session_id, candidate_auth):
    """A candidate cannot trigger reprocessing (403)."""
    # A candidate tries to reprocess.
    response = await client.post(
        f"/api/v1/sessions/{live_session_id}/transcript/reprocess", headers=candidate_auth
    )
    assert response.status_code == 403


async def test_segments_are_persisted_individually_as_they_arrive(session_factory, live_session_id):
    """claude.md invariant 2: no end-of-session batch write."""
    session_id = uuid.UUID(live_session_id)
    # Write one segment and commit.
    async with session_factory() as db:
        await speech_service.persist_segment(db, session_id, "A", 0.0, 5.0, "first", 0.9)
        await db.commit()
    # In a new database session it is already visible; write a second.
    async with session_factory() as db:
        # Visible immediately, before anything else happens to the session.
        assert len(await speech_repo.list_segments(db, session_id)) == 1
        await speech_service.persist_segment(db, session_id, "B", 6.0, 9.0, "second", 0.9)
        await db.commit()
    # Both are visible.
    async with session_factory() as db:
        assert len(await speech_repo.list_segments(db, session_id)) == 2


async def test_a_participant_cannot_upload_audio_for_someone_else(
    client: AsyncClient, live_session_id, candidate_auth, second_candidate
):
    """Attribution comes from the authenticated participant, never the payload."""
    # B uploads while claiming to be A in the form.
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


# A two-line scripted dialogue used by the seeding tests.
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
    # The host seeds the scripted transcript.
    response = await client.post(
        f"/api/v1/sessions/{live_session_id}/transcript/seed", json=SEED_BODY, headers=host_auth
    )
    assert response.status_code == 200, response.text
    assert response.json()["segments"] == 2

    # Both the transcript segments and the speech events were written.
    async with session_factory() as db:
        session_id = uuid.UUID(live_session_id)
        assert len(await speech_repo.list_segments(db, session_id)) == 2
        from services.session import repository as session_repo

        assert len(await session_repo.list_speech_events(db, session_id)) == 2


async def test_seeding_is_host_only(client: AsyncClient, live_session_id, candidate_auth):
    """Only the host can seed a transcript."""
    # A candidate tries to seed.
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
    # A segment from label Z, who is not in the session.
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
    """Seeding needs a live session."""
    # Join only; the session is not live.
    await client.post(
        "/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=candidate_auth
    )
    # Seeding before the session is live.
    response = await client.post(
        f"/api/v1/sessions/{gd_session['id']}/transcript/seed", json=SEED_BODY, headers=host_auth
    )
    assert response.status_code == 409


async def test_seeding_is_refused_outside_the_local_environment(
    client: AsyncClient, live_session_id, host_auth
):
    """Guarded twice — it must never be reachable where transcripts are evidence."""
    # Local import: this test flips the app environment.
    from services.common.config import settings

    # Pretend we are not running locally.
    settings.app_env = "production"
    # Attempt to seed, then always restore local.
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
