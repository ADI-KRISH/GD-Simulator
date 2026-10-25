"""Solo practice: session setup, the conversation loop, and barge-in tracking.

Purpose:     Tests practice mode end to end at the room level: creating a solo
             session, a spoken turn being recorded and answered, a student
             talking over a persona (an interruption), the clock phases and
             guidance from `practice/timing.py`, and personas waiting for or
             staying silent around the student.
Why needed:  These are the behaviours that make a practice session feel like a
             real discussion and feed correct data (turns, interruptions) into
             scoring. The timing tests also cover the phase boundaries.
Contains:    the `_fast_room` fixture, the `PRACTICE` request body, helpers
             `_live_practice` (create + start a session) and `_room` (a room that
             captures sent frames), then tests for: seating and persona labels,
             unknown preset rejected, spoken turn answered, barge-in recorded as
             an interruption, phase boundaries, short-session pacing, conclusion
             guidance, silence after the student concludes, waiting for the
             student to finish, and one voice at a time.
Used by:     pytest (run with `pytest services/tests/test_practice.py`).
Depends on:  practice/engine.py, practice/personas.py and practice/timing.py
             (under test), session/repository.py, speech/stt.py and speech/tts.py
             (stub providers); fixtures from tests/conftest.py.
"""

import asyncio
import uuid

import pytest
from httpx import AsyncClient

from services.practice import engine
from services.practice.engine import STUDENT_LABEL, PracticeRoom
from services.practice.personas import resolve_personas
from services.session import repository as session_repo
from services.speech.stt import StubSTT, set_stt_provider
from services.speech.tts import StubTTS, set_tts_provider


@pytest.fixture(autouse=True)
def _fast_room(monkeypatch):
    """No real browser reports playback in tests, so do not wait for one."""
    monkeypatch.setattr(engine, "PLAYBACK_SLACK_SECONDS", 0.0)
    monkeypatch.setattr(engine, "LISTEN_HOLD_SECONDS", 0.05)


PRACTICE = {
    "topic": "Should remote work be the default?",
    "duration_seconds": 300,
    "preparation_seconds": 0,
    "personas": [{"preset": "arjun"}, {"preset": "bhavana"}],
}


async def _live_practice(client: AsyncClient, auth: dict) -> dict:
    """Create a practice session, ready up, start it and wait until it is LIVE."""
    # Create the practice session over the real API.
    created = await client.post("/api/v1/practice", json=PRACTICE, headers=auth)
    assert created.status_code == 201, created.text
    session = created.json()
    sid = session["id"]
    # Ready up, then start; with no preparation time it goes straight to LIVE.
    assert (
        await client.post(f"/api/v1/lobbies/{sid}/ready", json={"ready": True}, headers=auth)
    ).status_code == 200
    started = await client.post(f"/api/v1/sessions/{sid}/start", headers=auth)
    assert started.json()["state"] == "LIVE"
    return session


def _room(session_factory, session_id, name="Asha"):
    """Build a PracticeRoom wired to capture the frames it sends."""
    # Everything the room would send to the browser is appended here for assertions.
    sent: list[dict] = []

    async def send_json(frame: dict) -> None:
        """Capture JSON frames."""
        sent.append(frame)

    async def send_bytes(chunk: bytes) -> None:
        """Capture the size of binary chunks."""
        sent.append({"bytes": len(chunk)})

    return PracticeRoom(session_factory, uuid.UUID(session_id), name, send_json, send_bytes), sent


async def test_practice_session_seats_the_student_and_the_personas(
    client: AsyncClient, candidate_auth: dict
):
    """A practice session seats the student as 'A' and stores personas with their own labels."""
    # The student is seated as participant "A" in a "solo" session.
    created = await client.post("/api/v1/practice", json=PRACTICE, headers=candidate_auth)
    body = created.json()
    assert body["mode"] == "solo"
    assert [p["candidate_label"] for p in body["participants"]] == ["A"]
    # Personas are stored with labels that cannot collide with a candidate's.
    assert [p["label"] for p in resolve_personas(PRACTICE["personas"])] == ["P1", "P2"]
    # One live seat per student applies to practice too.
    again = await client.post("/api/v1/practice", json=PRACTICE, headers=candidate_auth)
    assert again.status_code == 409


async def test_unknown_persona_preset_is_rejected(client: AsyncClient, candidate_auth: dict):
    """An unknown persona preset gives 422."""
    # Swap in a preset name that doesn't exist; validation must fail with 422.
    bad = {**PRACTICE, "personas": [{"preset": "nobody"}]}
    response = await client.post("/api/v1/practice", json=bad, headers=candidate_auth)
    assert response.status_code == 422


async def test_a_spoken_turn_is_recorded_and_a_persona_answers(
    client: AsyncClient, candidate_auth: dict, session_factory
):
    """A spoken turn is stored and a persona replies."""
    # Stub speech providers avoid any real STT/TTS; then start a live room.
    set_stt_provider(StubSTT())
    set_tts_provider(StubTTS())
    session = await _live_practice(client, candidate_auth)
    room, sent = _room(session_factory, session["id"])
    await room.load()

    # Simulate one spoken utterance, then wait for the personas' reply task to finish.
    await room.on_utterance_start()
    await asyncio.sleep(0.05)
    await room.on_utterance_end()
    await room.on_audio(b"some audio")
    assert room.burst is not None
    await asyncio.wait_for(room.burst, timeout=10)

    # The stored turns should start with the student, followed by at least one persona.
    async with session_factory() as db:
        turns = await session_repo.list_turns(db, room.session_id)
    speakers = [t.candidate_label for t in turns]
    assert speakers[0] == STUDENT_LABEL and any(s.startswith("P") for s in speakers[1:])
    assert any(f.get("type") == "PERSONA_AUDIO_START" for f in sent)
    await room.close()


async def test_cutting_a_persona_off_is_an_interruption(
    client: AsyncClient, candidate_auth: dict, session_factory
):
    """Talking over a persona is recorded as an interruption."""
    set_stt_provider(StubSTT())
    set_tts_provider(StubTTS())
    session = await _live_practice(client, candidate_auth)
    room, sent = _room(session_factory, session["id"])
    await room.load()

    # Let persona P1 start speaking in the background...
    persona_turn = asyncio.create_task(room._persona_turn("P1"))
    for _ in range(100):  # wait until the persona is mid-sentence
        if room.speaking:
            break
        await asyncio.sleep(0.05)
    assert room.speaking == "P1"

    # ...then the student barges in: playback must stop and P1's turn reports "cut off".
    await room.on_utterance_start()  # student talks over them
    assert await asyncio.wait_for(persona_turn, timeout=5) is True  # cut off
    assert any(f.get("type") == "STOP_AUDIO" for f in sent)
    await room.on_utterance_end()
    await room.on_audio(b"my interjection")

    # The student's stored turn must name P1 as the one they interrupted.
    async with session_factory() as db:
        turns = await session_repo.list_turns(db, room.session_id)
    student = next(t for t in turns if t.candidate_label == STUDENT_LABEL)
    assert student.interrupted_label == "P1"
    await room.close()


# --------------------------------------------------------------------------
# Clock awareness
# --------------------------------------------------------------------------


def test_phases_follow_the_clock():
    """Opening, middle, wrap-up, final and over follow the elapsed time."""
    from services.practice import timing as t

    # In a 300-second discussion, check one moment per phase.
    assert t.timing_for(10, 300).phase == t.OPENING
    assert t.timing_for(120, 300).phase == t.MIDDLE
    assert t.timing_for(250, 300).phase == t.WRAP_UP  # 50 s left
    assert t.timing_for(285, 300).phase == t.FINAL  # 15 s left
    assert t.timing_for(297, 300).phase == t.OVER
    # Long sessions start converging at 20% remaining, not just the last minute.
    assert t.timing_for(730, 900).phase == t.WRAP_UP


def test_short_sessions_keep_a_middle_and_the_middle_is_paced():
    """Short sessions keep a middle; the middle moves from building to testing the case."""
    from services.practice import timing as t

    # A 2-minute session is not half wrap-up: converging starts at 40% remaining.
    assert t.timing_for(60, 120).phase == t.MIDDLE
    assert t.timing_for(80, 120).phase == t.WRAP_UP
    # Early middle asks personas to build the case; late middle asks them to test it.
    early, late = t.timing_for(60, 300), t.timing_for(150, 300)
    assert "build the case" in t.guidance(early, concluded=False)
    assert "test the case" in t.guidance(late, concluded=False)
    assert t.timing_for(150, 300).lines_left == 10  # 150 s left / 14 s a line
    assert "about 10 more lines" in t.guidance(late, concluded=False)


def test_guidance_tells_personas_not_to_repeat_a_conclusion():
    """The clock guidance warns when someone has already concluded; conclusions are detected."""
    from services.practice import timing as t

    # In wrap-up, the guidance changes depending on whether someone already concluded.
    late = t.timing_for(250, 300)
    assert "ALREADY concluded" in t.guidance(late, concluded=True)
    assert "ALREADY" not in t.guidance(late, concluded=False)
    assert t.looks_like_conclusion("So, to sum up, we agree on flexibility.")
    assert not t.looks_like_conclusion("I think remote work is great.")


async def test_personas_stay_silent_once_the_student_has_concluded(
    client: AsyncClient, candidate_auth: dict, session_factory, monkeypatch
):
    """After the student concludes during wrap-up the personas say nothing more."""
    from services.practice import timing as t

    set_stt_provider(StubSTT())
    set_tts_provider(StubTTS())
    session = await _live_practice(client, candidate_auth)
    room, sent = _room(session_factory, session["id"])
    await room.load()

    # Pretend the discussion is in its closing minute and the student has wrapped up.
    async def closing():
        """Pretend the discussion is in its final stretch."""
        return t.timing_for(250, 300)

    # Force the room's clock to wrap-up and mark the discussion concluded, then trigger a reply.
    monkeypatch.setattr(room, "_timing", closing)
    room.concluded = True
    await room._burst()
    assert not any(f.get("type") == "PERSONA_AUDIO_START" for f in sent)
    await room.close()


async def test_personas_wait_for_the_student_to_finish(
    client: AsyncClient, candidate_auth: dict, session_factory
):
    """Personas hold their reply while the student is still talking."""
    set_stt_provider(StubSTT())
    set_tts_provider(StubTTS())
    session = await _live_practice(client, candidate_auth)
    room, sent = _room(session_factory, session["id"])
    await room.load()

    # While the student is "still talking", a reply attempt must say nothing.
    room.user_active = True  # the student is still mid-sentence
    room.last_speaker = STUDENT_LABEL
    task = asyncio.create_task(room._burst(hold=0.0))
    await asyncio.sleep(0.5)
    assert not any(f.get("type") == "PERSONA_AUDIO_START" for f in sent)

    # Once they stop, the same pending reply should go ahead.
    room.user_active = False  # they stop; now a persona may answer
    await asyncio.wait_for(task, timeout=10)
    assert any(f.get("type") == "PERSONA_AUDIO_START" for f in sent)
    await room.close()


async def test_a_new_burst_never_starts_over_a_live_line(
    client: AsyncClient, candidate_auth: dict, session_factory
):
    """A new reply is not started while a persona's line is in progress."""
    session = await _live_practice(client, candidate_auth)
    room, _ = _room(session_factory, session["id"])
    await room.load()
    # With a persona mid-line, asking for a new reply must be refused (one voice at a time).
    room.speaking = "P1"  # a persona is mid-line
    room._start_burst()
    assert room.burst is None  # refused: one voice at a time
