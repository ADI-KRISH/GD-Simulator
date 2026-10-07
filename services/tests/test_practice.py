"""Solo practice: session setup, the conversation loop, and barge-in tracking."""

import asyncio
import uuid

from httpx import AsyncClient

from services.practice.engine import STUDENT_LABEL, PracticeRoom
from services.practice.personas import resolve_personas
from services.session import repository as session_repo
from services.speech.stt import StubSTT, set_stt_provider
from services.speech.tts import StubTTS, set_tts_provider

PRACTICE = {
    "topic": "Should remote work be the default?",
    "duration_seconds": 300,
    "preparation_seconds": 0,
    "personas": [{"preset": "arjun"}, {"preset": "bhavana"}],
}


async def _live_practice(client: AsyncClient, auth: dict) -> dict:
    created = await client.post("/api/v1/practice", json=PRACTICE, headers=auth)
    assert created.status_code == 201, created.text
    session = created.json()
    sid = session["id"]
    assert (
        await client.post(f"/api/v1/lobbies/{sid}/ready", json={"ready": True}, headers=auth)
    ).status_code == 200
    started = await client.post(f"/api/v1/sessions/{sid}/start", headers=auth)
    assert started.json()["state"] == "LIVE"
    return session


def _room(session_factory, session_id, name="Asha"):
    sent: list[dict] = []

    async def send_json(frame: dict) -> None:
        sent.append(frame)

    async def send_bytes(chunk: bytes) -> None:
        sent.append({"bytes": len(chunk)})

    return PracticeRoom(session_factory, uuid.UUID(session_id), name, send_json, send_bytes), sent


async def test_practice_session_seats_the_student_and_the_personas(
    client: AsyncClient, candidate_auth: dict
):
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
    bad = {**PRACTICE, "personas": [{"preset": "nobody"}]}
    response = await client.post("/api/v1/practice", json=bad, headers=candidate_auth)
    assert response.status_code == 422


async def test_a_spoken_turn_is_recorded_and_a_persona_answers(
    client: AsyncClient, candidate_auth: dict, session_factory
):
    set_stt_provider(StubSTT())
    set_tts_provider(StubTTS())
    session = await _live_practice(client, candidate_auth)
    room, sent = _room(session_factory, session["id"])
    await room.load()

    await room.on_utterance_start()
    await asyncio.sleep(0.05)
    await room.on_utterance_end()
    await room.on_audio(b"some audio")
    assert room.burst is not None
    await asyncio.wait_for(room.burst, timeout=10)

    async with session_factory() as db:
        turns = await session_repo.list_turns(db, room.session_id)
    speakers = [t.candidate_label for t in turns]
    assert speakers[0] == STUDENT_LABEL and any(s.startswith("P") for s in speakers[1:])
    assert any(f.get("type") == "PERSONA_AUDIO_START" for f in sent)
    await room.close()


async def test_cutting_a_persona_off_is_an_interruption(
    client: AsyncClient, candidate_auth: dict, session_factory
):
    set_stt_provider(StubSTT())
    set_tts_provider(StubTTS())
    session = await _live_practice(client, candidate_auth)
    room, sent = _room(session_factory, session["id"])
    await room.load()

    persona_turn = asyncio.create_task(room._persona_turn("P1"))
    for _ in range(100):  # wait until the persona is mid-sentence
        if room.speaking:
            break
        await asyncio.sleep(0.05)
    assert room.speaking == "P1"

    await room.on_utterance_start()  # student talks over them
    assert await asyncio.wait_for(persona_turn, timeout=5) is True  # cut off
    assert any(f.get("type") == "STOP_AUDIO" for f in sent)
    await room.on_utterance_end()
    await room.on_audio(b"my interjection")

    async with session_factory() as db:
        turns = await session_repo.list_turns(db, room.session_id)
    student = next(t for t in turns if t.candidate_label == STUDENT_LABEL)
    assert student.interrupted_label == "P1"
    await room.close()


# --------------------------------------------------------------------------
# Clock awareness
# --------------------------------------------------------------------------


def test_phases_follow_the_clock():
    from services.practice import timing as t

    assert t.timing_for(10, 300).phase == t.OPENING
    assert t.timing_for(120, 300).phase == t.MIDDLE
    assert t.timing_for(250, 300).phase == t.WRAP_UP  # 50 s left
    assert t.timing_for(285, 300).phase == t.FINAL  # 15 s left
    assert t.timing_for(297, 300).phase == t.OVER
    # Long sessions start converging at 20% remaining, not just the last minute.
    assert t.timing_for(730, 900).phase == t.WRAP_UP


def test_guidance_tells_personas_not_to_repeat_a_conclusion():
    from services.practice import timing as t

    late = t.timing_for(250, 300)
    assert "ALREADY concluded" in t.guidance(late, concluded=True)
    assert "ALREADY" not in t.guidance(late, concluded=False)
    assert t.looks_like_conclusion("So, to sum up, we agree on flexibility.")
    assert not t.looks_like_conclusion("I think remote work is great.")


async def test_personas_stay_silent_once_the_student_has_concluded(
    client: AsyncClient, candidate_auth: dict, session_factory, monkeypatch
):
    from services.practice import timing as t

    set_stt_provider(StubSTT())
    set_tts_provider(StubTTS())
    session = await _live_practice(client, candidate_auth)
    room, sent = _room(session_factory, session["id"])
    await room.load()

    # Pretend the discussion is in its closing minute and the student has wrapped up.
    async def closing():
        return t.timing_for(250, 300)

    monkeypatch.setattr(room, "_timing", closing)
    room.concluded = True
    await room._burst()
    assert not any(f.get("type") == "PERSONA_AUDIO_START" for f in sent)
    await room.close()
