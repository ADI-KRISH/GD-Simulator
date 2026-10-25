"""AI agent participants (services/simulation.py), driven by the stub LLM.

Purpose:    verify the simulated-participant feature end to end (agents join,
            become ready, talk once the session is live, and stop when it ends)
            plus the small rules that decide who interrupts and which voice
            each agent gets.
Why needed: the simulation produces the transcripts used for demos and
            evaluation testing; if it misbehaves (agents never speak, someone
            interrupts themselves, voices collide) those demos silently break.
Contains:
    AGENTS                                         - sample request body with two agents.
    test_agents_join_ready_and_talk_once_live      - full flow through the API.
    test_only_the_host_can_add_agents              - permission check (403).
    _sim                                           - helper that builds a tiny simulation.
    test_cut_keeps_the_start_of_the_line_and_marks_the_break - interrupted-line trimming.
    test_nobody_cuts_in_on_a_short_line, test_a_pushy_agent_cuts_in_and_never_on_themselves,
    test_polite_agents_never_interrupt             - interruption rules.
    test_the_overlap_is_long_enough_to_count_as_an_interruption - overlap vs metrics threshold.
    test_agents_get_distinct_voices_and_presets_keep_theirs - voice assignment.
    test_a_voiced_line_takes_its_timing_from_the_audio, test_no_audio_provider_means_text_only -
        spoken-line timing and the text-only fallback.
Used by:    pytest.
Depends on: services (package), services/simulation, and at run time the
            fixtures in conftest.py plus speech/diarization, practice/personas
            and speech/tts.
"""

import asyncio
import uuid

from httpx import AsyncClient

from services import simulation

# A valid request: two agents with different personas and talkativeness.
AGENTS = {
    "agents": [
        {"name": "Arjun", "persona": "Dominates.", "talkativeness": "high"},
        {"name": "Chitra", "persona": "Quiet expert.", "talkativeness": "low"},
    ],
    "extra_prompt": "Keep it short.",
}


async def test_agents_join_ready_and_talk_once_live(
    client: AsyncClient, host_auth: dict, gd_session: dict, monkeypatch
):
    """AI agents join ready and talk once the session goes live."""
    # No pause between lines, so the test does not wait on the normal pacing.
    monkeypatch.setattr(simulation, "TURN_GAP_SECONDS", 0)
    sid = gd_session["id"]

    # Step 1: the host adds agents; they must appear as ready participants.
    added = await client.post(f"/api/v1/sessions/{sid}/agents", json=AGENTS, headers=host_auth)
    assert added.status_code == 200, added.text
    lobby = added.json()
    assert [p["display_name"] for p in lobby["participants"]] == ["Arjun (AI)", "Chitra (AI)"]
    assert lobby["start_condition_met"], "agents must arrive ready so the host can start"

    # Step 2: start the session; the background loop should now begin talking.
    started = await client.post(f"/api/v1/sessions/{sid}/start", headers=host_auth)
    assert started.status_code == 200, started.text

    # Step 3: poll the transcript until an agent line shows up (or give up).
    lines: list = []
    for _ in range(40):  # the loop polls for LIVE once a second
        await asyncio.sleep(0.25)
        transcript = await client.get(f"/api/v1/sessions/{sid}/transcript", headers=host_auth)
        lines = transcript.json()["lines"]
        if lines:
            break
    assert lines, "no agent spoke after the session went live"
    assert lines[0]["candidate_label"] in {"A", "B"}

    # Ending the session is what silences them.
    await client.post(f"/api/v1/sessions/{sid}/end", headers=host_auth)
    task = simulation._simulations[uuid.UUID(sid)].task
    await asyncio.wait_for(task, timeout=10)
    assert sid not in {str(k) for k in simulation._simulations}


async def test_only_the_host_can_add_agents(
    client: AsyncClient, gd_session: dict, candidate_auth: dict
):
    """A candidate cannot add agents (403)."""
    response = await client.post(
        f"/api/v1/sessions/{gd_session['id']}/agents", json=AGENTS, headers=candidate_auth
    )
    assert response.status_code == 403


def _sim(*specs: tuple[str, str, float]) -> simulation._Simulation:
    """Build a simulation with agents given as (label, name, interrupt probability)."""
    sim = simulation._Simulation()
    for label, name, interrupt_p in specs:
        sim.agents[label] = simulation._Agent(label, name, "persona", 1.0, interrupt_p)
    return sim


def test_cut_keeps_the_start_of_the_line_and_marks_the_break():
    """A cut line keeps its beginning and ends with an ellipsis."""
    # Cutting ten words at 50% should keep the first five and add the dash marker.
    text = "one two three four five six seven eight nine ten"
    cut = simulation._cut(text, 0.5)
    assert cut == "one two three four five —"


def test_nobody_cuts_in_on_a_short_line():
    """A short line is never interrupted."""
    sim = _sim(("A", "Arjun", 1.0), ("B", "Bhavana", 1.0))
    assert simulation._pick_interrupter(sim, sim.agents["A"], "too short to cut in on") is None


def test_a_pushy_agent_cuts_in_and_never_on_themselves():
    """A pushy agent interrupts others but never themselves."""
    # B always interrupts (p=1.0) and A never does (p=0.0); repeat to rule out luck.
    sim = _sim(("A", "Arjun", 0.0), ("B", "Bhavana", 1.0))
    long_line = " ".join(["word"] * 30)
    for _ in range(20):
        assert simulation._pick_interrupter(sim, sim.agents["A"], long_line) is sim.agents["B"]
        assert simulation._pick_interrupter(sim, sim.agents["B"], long_line) is None


def test_polite_agents_never_interrupt():
    """Agents with zero interrupt probability never cut in."""
    sim = _sim(("A", "Arjun", 0.0), ("B", "Bhavana", 0.0))
    long_line = " ".join(["word"] * 30)
    assert all(
        simulation._pick_interrupter(sim, sim.agents["A"], long_line) is None for _ in range(50)
    )


def test_the_overlap_is_long_enough_to_count_as_an_interruption():
    """Simulated overlaps exceed the interruption threshold."""
    from services.speech.diarization import MIN_OVERLAP_SECONDS

    # If the overlap were shorter than the threshold, metrics would not count it.
    assert min(simulation.OVERLAP_SECONDS) > MIN_OVERLAP_SECONDS


def test_agents_get_distinct_voices_and_presets_keep_theirs():
    """Agents get different voices; presets keep their own."""
    from services.practice.personas import PRESETS
    from services.simulation import VOICE_POOL, _voice_for

    # Assign voices one by one, feeding back the ones already taken.
    taken: list[str] = []
    for name in ("Dev", "Priya", "Rahul", "Anita"):
        taken.append(_voice_for(name, taken))
    assert len(set(taken)) == len(taken)
    assert taken[0] == PRESETS["dev"].voice_id
    assert all(v in {PRESETS[k].voice_id for k in VOICE_POOL} for v in taken)


async def test_a_voiced_line_takes_its_timing_from_the_audio(monkeypatch):
    """A spoken line's duration comes from its audio length."""
    from services import simulation
    from services.speech.tts import set_tts_provider

    class Fake:
        """Fake TTS producing a fixed amount of audio."""
        name = "fake"
        produces_audio = True
        mp3_bytes_per_second = 1_000

        async def stream(self, text, voice_id):
            """Yield two 4000-byte chunks."""
            yield b"x" * 4_000
            yield b"x" * 4_000

    # Install the fake voice, check 8000 bytes at 1000 B/s = 8 seconds, then restore.
    set_tts_provider(Fake())
    try:
        agent = simulation._Agent("A", "Dev", "p", 1.0, voice_id="v")
        audio, seconds = await simulation._speak(agent, "hello there")
        assert len(audio) == 8_000 and seconds == 8.0

        # Turning voices off in settings must fall back to text-only.
        monkeypatch.setattr(simulation.settings, "sim_voices", False)
        assert await simulation._speak(agent, "hello there") is None
    finally:
        set_tts_provider(None)


async def test_no_audio_provider_means_text_only():
    """Without an audio provider a line has no spoken audio."""
    from services import simulation
    from services.speech.tts import StubTTS, set_tts_provider

    # The stub TTS makes no audio, so _speak must return None.
    set_tts_provider(StubTTS())
    try:
        agent = simulation._Agent("A", "Dev", "p", 1.0, voice_id="v")
        assert await simulation._speak(agent, "hello") is None
    finally:
        set_tts_provider(None)
