"""The AI panel behaves as a panel (M14 FR-14.3 to FR-14.6, AC-14.5, EC-14.1/2/4).

Unit tests for the pure decisions in `services.practice.panel`, then the same
decisions inside a live practice room, with the model replaced by a script so a
test pins what the room asked for rather than what a model happened to say.
"""

import asyncio
import random
import uuid

import pytest
from httpx import AsyncClient

from services.practice import engine, panel
from services.practice.engine import Line, PracticeRoom
from services.practice.personas import PRESETS, resolve_personas
from services.speech.stt import StubSTT, set_stt_provider
from services.speech.tts import StubTTS, set_tts_provider

PERSONAS = {
    "P1": {"label": "P1", "name": "Arjun", "persona": "Analytical.", "eagerness": 1.0,
           "stance": "support", "angle": "cost"},
    "P2": {"label": "P2", "name": "Bhavana", "persona": "Challenges every claim.",
           "eagerness": 1.3, "stance": "oppose", "angle": "fairness"},
    "P3": {"label": "P3", "name": "Chitra", "persona": "Calm moderator. Summarises.",
           "eagerness": 1.0, "stance": "it depends", "angle": "students"},
}  # fmt: skip


# ------------------------------------------------------------ the panel


def test_up_to_three_personas_never_share_a_stance():
    for seed in range(50):
        rng = random.Random(seed)
        for count in (2, 3):
            stances = panel.sample_stances(count, rng)
            assert len(set(stances)) == count


def test_four_personas_cover_every_stance():
    for seed in range(50):
        assert set(panel.sample_stances(4, random.Random(seed))) == set(panel.STANCES)


def test_no_two_personas_share_an_angle():
    for seed in range(50):
        angles = panel.sample_angles(4, random.Random(seed))
        assert len(set(angles)) == 4


def test_every_persona_row_carries_a_stance_and_an_angle():
    rows = resolve_personas(
        [{"preset": "arjun"}, {"preset": "bhavana"}, {"preset": "chitra"}], random.Random(1)
    )
    assert len({row["stance"] for row in rows}) == 3
    assert all(row["angle"] in {name for name, _ in panel.ANGLES} for row in rows)


def test_the_analyst_no_longer_demands_a_number_for_every_point():
    assert "number" not in PRESETS["arjun"].brief.split("never makes up")[0]
    assert "never makes up a number" in PRESETS["arjun"].brief


def test_the_natural_moderator_runs_the_room():
    assert panel.pick_moderator(PERSONAS) == "P3"


def test_without_a_natural_moderator_the_first_persona_runs_it():
    plain = {k: {**v, "persona": "Speaks plainly."} for k, v in PERSONAS.items()}
    assert panel.pick_moderator(plain) == "P1"


# ------------------------------------------------------------- prompts


def _system(label="P1", **kwargs):
    return panel.persona_system(
        PERSONAS[label],
        student="Asha",
        others="the others",
        topic="Mandatory internships",
        max_words=35,
        clock_guidance="CLOCK: ...",
        is_moderator=label == "P3",
        **kwargs,
    )


def test_every_persona_is_told_it_is_an_ai_and_never_to_invent_figures():
    system = _system()
    assert "AI participant" in system and "Never claim to be human" in system
    assert "Never invent statistics" in system


def test_the_prompt_carries_the_stance_and_the_angle():
    system = _system()
    assert "SUPPORT the motion" in system and "cost —" in system


def test_only_the_moderator_is_given_the_room():
    assert "keep this discussion fair" in _system("P3")
    assert "keep this discussion fair" not in _system("P1")


def test_the_opening_cue_invites_the_student_by_name():
    assert "invite Asha by name" in _system("P3", cue="open")


def test_a_redirect_names_the_persona_handed_the_floor():
    assert "hand the floor to Bhavana" in _system("P3", cue="redirect", target="Bhavana")


def test_the_json_instruction_comes_last():
    assert _system(cue="curveball").rstrip().endswith('"<what you say next>"}.')


# ------------------------------------------------------------ the floor


def test_a_restated_point_is_caught():
    earlier = ["Mandatory internships burden poor students who must travel to cities for work."]
    again = "Making internships mandatory would burden poor students who travel to cities."
    assert panel.repeats(again, earlier) == earlier[0]


def test_a_new_angle_is_not_a_repeat():
    earlier = ["Mandatory internships burden poor students who must travel to cities for work."]
    fresh = "Employers might hire interns cheaply instead of paying graduates a fair salary."
    assert panel.repeats(fresh, earlier) == ""


def test_a_short_agreement_is_not_a_repeat():
    assert panel.repeats("Yes, I agree with that.", ["I agree with that point entirely."]) == ""


def test_the_one_who_just_spoke_never_bids_again():
    for seed in range(30):
        chosen = panel.choose_next(
            PERSONAS,
            last_speaker="P2",
            last_text="",
            turn=5,
            last_turn_of={"P1": 4, "P2": 5},
            floor={},
            moderator="P3",
            closing=False,
            rng=random.Random(seed),
        )
        assert chosen != "P2"


def test_a_persona_who_has_not_spoken_outbids_one_who_just_did():
    wins = sum(
        panel.choose_next(
            {k: PERSONAS[k] for k in ("P1", "P2")},
            last_speaker="A",
            last_text="",
            turn=6,
            last_turn_of={"P2": 5},
            floor={"P2": 30.0},
            moderator=None,
            closing=False,
            rng=random.Random(seed),
        )
        == "P1"
        for seed in range(50)
    )
    assert wins == 50


def test_the_moderator_takes_the_close():
    for seed in range(30):
        assert (
            panel.choose_next(
                PERSONAS,
                last_speaker="P1",
                last_text="",
                turn=9,
                last_turn_of={"P1": 9, "P2": 8, "P3": 7},
                floor={"P1": 20, "P2": 20, "P3": 20},
                moderator="P3",
                closing=True,
                rng=random.Random(seed),
            )
            == "P3"
        )


def test_a_student_holding_most_of_the_talk_is_redirected_at_most_twice():
    floor = {"A": 80.0, "P1": 10.0, "P2": 10.0}
    assert panel.should_redirect(floor, redirects=0)
    assert panel.should_redirect(floor, redirects=1)
    assert not panel.should_redirect(floor, redirects=2)


def test_no_redirect_before_there_is_enough_talk_to_judge():
    assert not panel.should_redirect({"A": 30.0, "P1": 2.0}, redirects=0)


def test_the_floor_goes_to_the_quietest_persona():
    assert panel.quietest(PERSONAS, {"P1": 12.0, "P2": 3.0, "P3": 1.0}, exclude="P3") == "P2"


# ------------------------------------------------------------- honesty


@pytest.mark.parametrize(
    "question",
    ["Are you a bot?", "wait, r u an AI", "Are you a real person?", "are you human"],
)
def test_asked_if_it_is_an_ai_the_reply_says_so(question):
    reply = panel.ensure_honest("Good point, but who pays for it?", question)
    assert reply.startswith("I'm an AI participant")


def test_a_reply_that_already_admits_it_is_left_alone():
    reply = "Yes, I'm an AI participant. Anyway, who pays for it?"
    assert panel.ensure_honest(reply, "Are you a bot?") == reply


def test_no_question_no_change():
    assert panel.ensure_honest("Who pays for it?", "I think costs matter.") == "Who pays for it?"


# ------------------------------------------------- inside a practice room


@pytest.fixture(autouse=True)
def _fast_room(monkeypatch):
    monkeypatch.setattr(engine, "PLAYBACK_SLACK_SECONDS", 0.0)
    monkeypatch.setattr(engine, "LISTEN_HOLD_SECONDS", 0.05)
    # No browser reports playback here, so a line "plays" for its estimated length;
    # and a random second persona chaining on made these tests flaky past 10 s.
    monkeypatch.setattr(engine, "estimate_seconds", lambda _text: 0.3)
    monkeypatch.setattr(engine, "CHAIN_PROBABILITY", 0.0)


async def _room(client: AsyncClient, auth: dict, session_factory):
    set_stt_provider(StubSTT())
    set_tts_provider(StubTTS())
    created = await client.post(
        "/api/v1/practice",
        json={
            "topic": "Should internships be mandatory for graduation?",
            "duration_seconds": 300,
            "preparation_seconds": 0,
            "personas": [{"preset": "arjun"}, {"preset": "bhavana"}, {"preset": "chitra"}],
        },
        headers=auth,
    )
    sid = created.json()["id"]
    await client.post(f"/api/v1/lobbies/{sid}/ready", json={"ready": True}, headers=auth)
    await client.post(f"/api/v1/sessions/{sid}/start", headers=auth)
    sent: list[dict] = []

    async def send_json(frame):
        sent.append(frame)

    async def send_bytes(chunk):
        sent.append({"bytes": len(chunk)})

    room = PracticeRoom(session_factory, uuid.UUID(sid), "Asha", send_json, send_bytes)
    await room.load()
    return room, sent


def _script(room, *texts):
    """Replace the model: record (label, cue) per request, answer from `texts` in turn."""
    calls: list[tuple[str, str | None]] = []
    replies = list(texts)

    async def fake_generate(persona, when, cue=None, target="", repeated=""):
        calls.append((persona["label"], cue))
        text = replies.pop(0) if replies else f"A fresh thought number {len(calls)} entirely."
        return Line(speak=True, text=text, is_conclusion=False)

    room._generate = fake_generate
    return calls


async def test_the_moderator_opens_and_hands_the_floor_to_the_student(
    client, candidate_auth, session_factory
):
    room, _ = await _room(client, candidate_auth, session_factory)
    calls = _script(room, "Let's begin. Asha, what is your view?")
    await room._burst(opening=True)
    assert calls == [(room.moderator, "open")], "one invitation, then the student's floor"
    assert room.personas[room.moderator]["name"] == "Chitra"
    await room.close()


async def test_a_dominant_student_is_redirected_to_the_quietest_persona(
    client, candidate_auth, session_factory
):
    room, _ = await _room(client, candidate_auth, session_factory)
    others = [label for label in room.personas if label != room.moderator]
    room.floor = {"A": 60.0, others[0]: 6.0, others[1]: 2.0, room.moderator: 1.0}
    calls = _script(room)
    await room.on_utterance_start()
    await asyncio.sleep(0.05)
    await room.on_utterance_end()
    await room.on_audio(b"some audio")
    await asyncio.wait_for(room.burst, timeout=10)
    assert calls[0] == (room.moderator, "redirect")
    assert calls[1] == (others[1], None), "the persona the moderator named answers"
    assert room.redirects == 1
    await room.close()


async def test_a_repeated_point_goes_back_once_then_the_persona_yields(
    client, candidate_auth, session_factory
):
    room, sent = await _room(client, candidate_auth, session_factory)
    said = "Mandatory internships burden poor students who must travel to cities for work."
    room.lines = [("Arjun", said)]
    calls = _script(room, said, said)
    label = next(label for label in room.personas if label != room.moderator)
    assert await room._persona_turn(label) is False
    assert calls == [(label, None), (label, "fresh")]
    assert room.yields == 1
    assert not any(f.get("type") == "PERSONA_AUDIO_START" for f in sent), "nothing was said"
    await room.close()


async def test_three_yields_in_a_row_bring_a_curveball(client, candidate_auth, session_factory):
    room, _ = await _room(client, candidate_auth, session_factory)
    said = "Mandatory internships burden poor students who must travel to cities for work."
    room.lines = [("Arjun", said)]
    _script(room, *[said] * 6)
    label = next(label for label in room.personas if label != room.moderator)
    for _ in range(3):
        await room._persona_turn(label)
    assert room.forced == (room.moderator, "curveball", "")
    await room.close()


async def test_asked_if_it_is_an_ai_the_next_line_says_so(client, candidate_auth, session_factory):
    room, sent = await _room(client, candidate_auth, session_factory)
    room.ai_question = "Hang on, are you a bot?"
    _script(room, "Fair question, but who pays for these internships?")
    await room._persona_turn(next(iter(room.personas)))
    spoken = next(f for f in sent if f.get("type") == "PERSONA_AUDIO_START")
    assert spoken["payload"]["text"].startswith("I'm an AI participant")
    await room.close()


def test_when_asked_the_turn_itself_says_to_admit_it():
    """The standing rule alone got an admission 2 times in 5 on Groq; this got 6 in 6."""
    assert "just asked whether you are an AI" in _system(asked_ai=True)
    assert "just asked whether you are an AI" not in _system()


async def test_a_line_the_student_cut_does_not_swallow_the_reply(
    client, candidate_auth, session_factory
):
    """Seen live: the reply to a barge-in waited 14.6 s for the idle nudge."""
    room, _ = await _room(client, candidate_auth, session_factory)
    calls = _script(room)
    room.speaking = "P1"  # a line the student cut, still unwinding
    room.barge.set()
    room._start_burst()
    assert room.burst is not None, "a cut line no longer blocks the reply"
    await asyncio.sleep(0.3)
    room.speaking = None  # the cut line finishes unwinding
    await asyncio.wait_for(room.burst, timeout=10)
    assert calls, "the student's turn was answered"
    await room.close()


# ------------------------------------------------- typed turns, raised hands


async def test_a_typed_turn_is_recorded_and_answered(client, candidate_auth, session_factory):
    """FR-14.19: the same scheduler and scoring as a spoken turn, minus speech-to-text."""
    from services.session import repository as session_repo

    room, _ = await _room(client, candidate_auth, session_factory)
    calls = _script(room)
    await room.on_text("I think internships teach what lectures cannot.")
    await asyncio.wait_for(room.burst, timeout=10)
    assert room.lines[0] == ("Asha", "I think internships teach what lectures cannot.")
    assert calls, "a persona answered the typed turn"
    async with session_factory() as db:
        turns = await session_repo.list_turns(db, room.session_id)
    student = [t for t in turns if t.candidate_label == "A"]
    assert len(student) == 1 and student[0].interrupted_label is None
    assert student[0].end - student[0].start == pytest.approx(0.3), "as long as saying it takes"
    await room.close()


async def test_typing_never_cuts_a_persona_off(client, candidate_auth, session_factory):
    room, _ = await _room(client, candidate_auth, session_factory)
    _script(room)
    room.speaking = "P1"  # a persona mid-line, not cut
    typed = asyncio.create_task(room.on_text("Wait, who actually pays for all this?"))
    await asyncio.sleep(0.3)
    assert room.lines == [], "the typed turn waits for the line to finish"
    room.speaking = None
    await asyncio.wait_for(typed, timeout=10)
    assert room.lines[-1][1] == "Wait, who actually pays for all this?"
    await room.close()


async def test_a_one_word_typed_turn_is_ignored(client, candidate_auth, session_factory):
    room, _ = await _room(client, candidate_auth, session_factory)
    await room.on_text("ok")
    assert room.lines == []
    await room.close()


async def test_a_typed_are_you_a_bot_gets_an_honest_answer(client, candidate_auth, session_factory):
    room, sent = await _room(client, candidate_auth, session_factory)
    _script(room, "Good question, but let's get back to the cost.")
    await room.on_text("Hang on, are you a bot?")
    await asyncio.wait_for(room.burst, timeout=10)
    spoken = next(f for f in sent if f.get("type") == "PERSONA_AUDIO_START")
    assert spoken["payload"]["text"].startswith("I'm an AI participant")
    await room.close()


async def test_a_raised_hand_brings_the_moderators_invitation(
    client, candidate_auth, session_factory
):
    """FR-14.12: the moderator invites the student, then the floor is theirs."""
    room, _ = await _room(client, candidate_auth, session_factory)
    calls = _script(room, "Asha, you had your hand up. Go ahead.")
    await room.on_raise_hand()
    await asyncio.wait_for(room.burst, timeout=10)
    assert calls == [(room.moderator, "hand")]
    assert room.hands == 1
    await room.close()


def test_the_hand_cue_invites_by_name():
    assert "has raised their hand" in _system("P3", cue="hand")


async def test_starting_with_no_preparation_tells_the_room_the_discussion_is_on(
    client, candidate_auth, session_factory, monkeypatch
):
    """Seen in the browser: with no preparation the page stayed on "Preparation"."""
    from services.practice import engine as engine_module

    created = await client.post(
        "/api/v1/practice",
        json={
            "topic": "Should internships be mandatory for graduation?",
            "duration_seconds": 120,
            "preparation_seconds": 0,
            "personas": [{"preset": "arjun"}],
        },
        headers=candidate_auth,
    )
    sid = created.json()["id"]
    frames: list[dict] = []

    async def capture(session_id, frame):
        frames.append(frame)

    monkeypatch.setattr(engine_module.hub, "broadcast", capture)
    monkeypatch.setattr("services.realtime.timer.hub.broadcast", capture)
    room = PracticeRoom(session_factory, uuid.UUID(sid), "Asha", _noop, _noop)
    await room.load()
    me = await client.get("/api/v1/auth/me", headers=candidate_auth)
    from services.auth import repository as auth_repo

    async with session_factory() as db:
        user = await auth_repo.get_user_by_id(db, uuid.UUID(me.json()["id"]))
    await room.start_session(user)
    kinds = [(f["type"], f["payload"].get("phase")) for f in frames]
    assert ("SESSION_STARTED", "discussion") in kinds
    assert any(f["type"] == "TOPIC_RELEASED" for f in frames)
    assert any(f["type"] == "TIMER_STARTED" for f in frames)
    await room.close()


async def _noop(*_args):
    return None


async def test_a_hand_raised_mid_line_is_invited_when_the_line_ends(
    client, candidate_auth, session_factory
):
    """Seen live: a hand raised while a persona spoke was never invited."""
    room, _ = await _room(client, candidate_auth, session_factory)
    calls: list[tuple[str, str | None]] = []

    async def fake_generate(persona, when, cue=None, target="", repeated=""):
        calls.append((persona["label"], cue))
        if len(calls) == 1:
            await room.on_raise_hand()  # the hand goes up while this line is made
        return Line(
            speak=True, text=f"A distinct thought number {len(calls)} here.", is_conclusion=False
        )

    room._generate = fake_generate
    await room._burst()
    assert calls[1] == (room.moderator, "hand")
    await room.close()


async def test_speaking_after_raising_a_hand_cancels_the_invitation(
    client, candidate_auth, session_factory
):
    """Seen live: the student typed before the invitation came; it must not follow."""
    room, _ = await _room(client, candidate_auth, session_factory)
    calls = _script(room)
    room.forced = (room.moderator, "hand", "")  # a hand raised, not yet invited
    await room.on_text("Actually, the cost should be shared by colleges and companies.")
    await asyncio.wait_for(room.burst, timeout=10)
    assert (room.moderator, "hand") not in calls
    await room.close()
