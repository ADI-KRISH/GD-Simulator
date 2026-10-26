"""AI agent participants for demos and evaluation testing.

A host adds a few LLM-played candidates to a lobby, each with a persona. They
join as ordinary participants, are marked ready, and once the session goes LIVE
they take turns speaking in real time: each line is written as a transcript
segment and a speech event, exactly as the speech pipeline would write it. So
everything downstream — live transcript, Observer, metrics, final evaluation —
runs unmodified, and the agents stop when the session clock ends.

Like `/transcript/seed`, this is refused unless `APP_ENV=local`: transcripts are
evidence, and nothing here may be reachable where they count.

ponytail: personas and running loops live in process memory. A backend restart
mid-session silences the agents; the session itself carries on.

Purpose:    let a host fill a lobby with AI "candidates" that really talk, so the
            whole pipeline can be demoed and tested without gathering people.
Why needed: evaluation needs realistic multi-person transcripts (turns,
            interruptions, overlaps); this produces them on demand through the
            same code path real speech uses.
Contains:
    AgentSpec / AgentsRequest - request shapes for adding agents (name, persona,
        talkativeness, how often they interrupt).
    _Agent, _Simulation, _Turn - in-memory state: one agent, one running
        simulation, one prepared line (text, voice, optional interruption).
    add_agents (POST /{session_id}/agents) - creates agent accounts, seats them,
        marks them ready and starts the background loop.
    _voice_for / _speak - choose a distinct voice per agent and synthesise audio.
    _pick_speaker / _pick_interrupter / _cut - decide who speaks next, who cuts in,
        and where the interrupted sentence is cut off.
    _say - ask the LLM for one in-character line (or an interjection).
    _prepare - write and voice the next line ahead of time (hides LLM latency).
    _run - the loop: wait for LIVE, record each turn like real speech, broadcast
        it, then wait as long as the line would take to say.
    _broadcast / _stop_later / _duration_of - small helpers for events and timing.
Used by:    main (mounts the router) and tests/test_simulation.
Depends on: auth (deps, models, repository), common (config, db, errors,
            security), llm, practice (panel rules, personas), realtime (hub,
            protocol), session (service, schemas, state), speech (service, tts).
"""

import asyncio  # background simulation tasks and pauses between lines
import base64  # embed mp3 bytes in a WebSocket JSON frame
import logging  # warn when a line or voice fails
import random  # pick speakers and cut-in timing
import secrets  # throwaway passwords for simulated accounts
import uuid  # ids for sessions and simulated users
from dataclasses import dataclass, field  # in-memory simulation state
from typing import Annotated  # attach Depends metadata

from fastapi import APIRouter, Depends  # routing and dependency injection
from pydantic import BaseModel, Field  # request models
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # DB session types

from services.auth.deps import CurrentUser, SessionDep  # injected user and DB session
from services.auth.models import Profile, Role, User  # account tables for simulated candidates
from services.auth.repository import create_user  # insert a simulated account
from services.common.config import settings  # sim_voices toggle
from services.common.db import get_session_factory  # sessions for background tasks
from services.common.errors import ConflictError  # 409
from services.common.security import hash_password  # hash the throwaway password
from services.llm import LLMError, get_llm_provider  # model that writes each simulated line
from services.practice.panel import RULES  # honesty rules every AI participant keeps
from services.practice.personas import PRESETS  # built-in persona briefs and voices
from services.realtime.hub import hub  # broadcast to the room
from services.realtime.protocol import ServerEvent, server_frame  # WebSocket event frames
from services.session import service as session_service  # record speech, lobby view
from services.session.schemas import LobbyView  # lobby response shape
from services.session.state import SessionState  # state checks
from services.speech import service as speech_service  # persist transcript segments
from services.speech.tts import TTSError, get_tts_provider  # optional spoken voices

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/sessions", tags=["simulation"])

WORDS_PER_SECOND = 2.6
# Gap between one agent finishing and the next starting, on top of the LLM call.
TURN_GAP_SECONDS = 1.0
# How long a configured-but-unstarted lobby keeps its agents waiting.
MAX_LOBBY_WAIT_SECONDS = 2 * 60 * 60
TALKATIVENESS = {"low": 1.0, "medium": 2.0, "high": 4.0}
# Chance that this agent cuts in on a speaker who is mid-sentence, per turn.
INTERRUPTIVENESS = {"low": 0.04, "medium": 0.15, "high": 0.35}
# A line shorter than this is over before anyone could cut in.
MIN_WORDS_TO_INTERRUPT = 14
# The speaker keeps talking for a moment after being cut in on, so the two
# really overlap. Must exceed diarization.MIN_OVERLAP_SECONDS to count.
OVERLAP_SECONDS = (0.9, 1.8)

# Voices handed to agents that are not named after a preset persona, alternating
# male and female so neighbours differ. Each is a preset's voice and delivery.
VOICE_POOL = ("arjun", "bhavana", "dev", "esha", "farhan", "chitra")

LINE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["text"],
    "properties": {"text": {"type": "string"}},
}


class AgentSpec(BaseModel):
    """One AI participant the host wants in the simulation: a name and a persona brief."""

    name: str = Field(min_length=1, max_length=60)
    persona: str = Field(min_length=1, max_length=2000)
    talkativeness: str = Field(default="medium", pattern="^(low|medium|high)$")
    # How readily they cut in on someone who is still speaking.
    interrupts: str = Field(default="medium", pattern="^(low|medium|high)$")


class AgentsRequest(BaseModel):
    """Body for adding AI participants to a lobby."""

    agents: list[AgentSpec] = Field(min_length=1, max_length=8)
    extra_prompt: str = Field(default="", max_length=2000)


@dataclass(slots=True)
class _Agent:
    """A live simulated participant: its label, name, persona and voice."""

    label: str
    name: str
    persona: str
    weight: float
    interrupt_p: float = 0.15
    voice_id: str = ""


@dataclass(slots=True)
class _Simulation:
    """In-memory state of one running simulation (its agents and conversation settings)."""

    agents: dict[str, _Agent] = field(default_factory=dict)
    extra_prompt: str = ""
    task: asyncio.Task | None = None


_simulations: dict[uuid.UUID, _Simulation] = {}


@router.post("/{session_id}/agents", response_model=LobbyView)
async def add_agents(
    session_id: uuid.UUID,
    payload: AgentsRequest,
    db: SessionDep,
    user: CurrentUser,
    factory: Annotated[async_sessionmaker[AsyncSession], Depends(get_session_factory)],
) -> LobbyView:
    """Seat AI agents in the lobby. They speak once the discussion is live."""
    # Safety: simulated transcripts must never be possible outside local use.
    if settings.app_env != "local":
        raise ConflictError("AI agents are only available in the local environment")
    # Only the session's host may add agents.
    gd = await session_service.require_session(db, session_id)
    session_service.require_host(gd, user)

    sim = _simulations.setdefault(session_id, _Simulation())
    sim.extra_prompt = payload.extra_prompt.strip()
    # For each requested agent: make a real (but un-loginable) account, seat it
    # in the lobby and mark it ready, so it is an ordinary participant.
    for spec in payload.agents:
        bot = await create_user(
            db,
            User(
                email=f"agent-{uuid.uuid4().hex[:12]}@agents.local",
                full_name=f"{spec.name.strip()} (AI)",
                # Nobody can log in as an agent: the password is never stored.
                password_hash=hash_password(secrets.token_urlsafe(32)),
                role=Role.CANDIDATE.value,
            ),
            Profile(),
        )
        _, participant = await session_service.join_by_code(db, bot, gd.code)
        await session_service.set_ready(db, gd, bot, True)
        sim.agents[participant.candidate_label] = _Agent(
            label=participant.candidate_label,
            name=spec.name.strip(),
            persona=spec.persona.strip(),
            weight=TALKATIVENESS[spec.talkativeness],
            interrupt_p=INTERRUPTIVENESS[spec.interrupts],
            voice_id=_voice_for(spec.name, [a.voice_id for a in sim.agents.values()]),
        )

    # Persist, then tell everyone in the lobby that the headcount changed.
    view = await session_service.lobby_view(db, gd)
    await db.commit()
    await hub.broadcast(
        str(gd.id),
        server_frame(ServerEvent.USER_JOINED, str(gd.id), {"occupancy": view.occupancy}),
    )
    # Start (or restart) the single background loop that makes the agents talk.
    if sim.task is None or sim.task.done():
        sim.task = asyncio.create_task(_run(factory, session_id), name=f"agents-{session_id}")
    return view


def _voice_for(name: str, taken: list[str]) -> str:
    """An agent named after a preset persona keeps that persona's voice; the rest get
    the next unused voice from the pool so no two agents share one."""
    # A preset name (e.g. "Arjun") keeps its own voice if nobody has it yet.
    preset = PRESETS.get(name.strip().lower())
    if preset is not None and preset.voice_id not in taken:
        return preset.voice_id
    # Otherwise hand out the first pool voice nobody is using.
    for key in VOICE_POOL:
        if PRESETS[key].voice_id not in taken:
            return PRESETS[key].voice_id
    # Everyone is taken: reuse voices round-robin rather than fail.
    return PRESETS[VOICE_POOL[len(taken) % len(VOICE_POOL)]].voice_id


async def _speak(agent: _Agent, text: str) -> tuple[bytes, float] | None:
    """The line as spoken audio and its true length, or None to fall back to text only."""
    # No voice available or wanted: the line stays text-only.
    tts = get_tts_provider()
    if not settings.sim_voices or not tts.produces_audio or not agent.voice_id:
        return None
    # Collect the streamed mp3 chunks; a voice failure must not break the turn.
    try:
        audio = b"".join([chunk async for chunk in tts.stream(text, agent.voice_id)])
    except TTSError as exc:
        logger.warning("agent voice failed", extra={"agent": agent.name, "error": str(exc)})
        return None
    if not audio:
        return None
    # The real spoken length drives the timing, not a word-count guess.
    return audio, len(audio) / tts.mp3_bytes_per_second


def _pick_speaker(sim: _Simulation, history: list[str]) -> _Agent:
    """Choose who talks next, weighted by talkativeness and recent silence."""
    agents = list(sim.agents.values())
    # Talkativeness skews the floor, but nobody goes silent for long: a silent
    # agent would score zero everywhere and test nothing.
    window = 2 * len(agents)
    for agent in agents:
        if len(history) >= window and agent.label not in history[-window:]:
            return agent
    # Otherwise pick at random by weight, never the same agent twice in a row.
    candidates = [a for a in agents if not history or a.label != history[-1]] or agents
    return random.choices(candidates, weights=[a.weight for a in candidates])[0]


async def _say(
    sim: _Simulation,
    agent: _Agent,
    topic: str,
    lines: list[tuple[str, str]],
    cutting_in_on: tuple[str, str] | None = None,
) -> str:
    """Have one agent say a line: ask the LLM, record it, optionally speak it, and broadcast."""
    # Context for the model: who else is present and the last 20 lines said.
    others = ", ".join(a.name for a in sim.agents.values() if a.label != agent.label)
    history = "\n".join(f"{who}: {text}" for who, text in lines[-20:]) or "(nobody has spoken yet)"
    # System prompt: the agent's identity, topic, persona, shared honesty rules
    # and how to behave like a real participant in a live discussion.
    system = (
        f"You are {agent.name}, a candidate in a live group discussion with {others}.\n"
        f"Topic: {topic}\n\nYour persona:\n{agent.persona}\n"
        + (f"\nAdditional instructions:\n{sim.extra_prompt}\n" if sim.extra_prompt else "")
        + f"\n{RULES}\n"
        + "\nThis is a live group discussion, so behave like a real participant: respond "
        "directly to what was just said, by name when you agree or disagree, build on or "
        "challenge a specific point, and stay on the topic without repeating what has been "
        "said. Speak in a few sentences at most, as spoken words — no lists, no markdown, no "
        'stage directions, no name prefix. Return JSON: {"text": "<what you say next>"}.'
    )
    # A normal turn asks "what next?"; an interruption quotes the partial sentence.
    if cutting_in_on is None:
        prompt = f"Discussion so far:\n{history}\n\nWhat do you say next?"
    else:
        who, partial = cutting_in_on
        system += (
            f"\nRight now {who} is still speaking and you are cutting in before they finish. "
            "Interject in one or two short sentences: object, add a point, or redirect."
        )
        prompt = (
            f"Discussion so far:\n{history}\n\n{who} is saying, and has got as far as: "
            f'"{partial}"\n\nWhat do you say as you cut in?'
        )
    # Ask for schema-checked JSON so the reply is always a clean line of text.
    result = await get_llm_provider().complete_json(
        system=system,
        prompt=prompt,
        schema=LINE_SCHEMA,
        max_tokens=400,
    )
    return str(result.data.get("text", "")).strip().strip('"')


def _pick_interrupter(sim: _Simulation, speaker: _Agent, text: str) -> _Agent | None:
    """Someone else may cut in on a long enough line, more likely if they are pushy."""
    # A short line ends before anyone could plausibly interrupt it.
    if len(text.split()) < MIN_WORDS_TO_INTERRUPT:
        return None
    # Visit the others in random order; each rolls against their own pushiness.
    for other in random.sample(list(sim.agents.values()), len(sim.agents)):
        if other.label != speaker.label and random.random() < other.interrupt_p:
            return other
    return None


def _cut(text: str, fraction: float) -> str:
    """What the speaker had said by the time they were cut in on."""
    # Keep at least three words, trim trailing punctuation, mark the break with a dash.
    words = text.split()
    kept = max(3, int(len(words) * fraction))
    return " ".join(words[:kept]).rstrip(",;:") + " —"


async def _stop_later(session_id: uuid.UUID, label: str, delay: float) -> None:
    """After `delay` seconds, broadcast that this agent stopped speaking."""
    await asyncio.sleep(delay)
    await _broadcast(session_id, ServerEvent.SPEECH_STOPPED, {"candidate_label": label})


async def _broadcast(session_id: uuid.UUID, event: ServerEvent, payload: dict) -> None:
    """Send one WebSocket event to everyone in the session's room."""
    await hub.broadcast(str(session_id), server_frame(event, str(session_id), payload))


@dataclass(slots=True)
class _Turn:
    """One agent's line, fully prepared: words, voice and any interruption."""

    agent: _Agent
    text: str
    duration: float
    spoken: tuple[bytes, float] | None = None
    # (interrupter, their line, seconds into this line they start, their audio)
    cut_in: tuple[_Agent, str, float, tuple[bytes, float] | None] | None = None


async def _prepare(
    sim: _Simulation, topic: str, lines: list[tuple[str, str]], history: list[str]
) -> _Turn:
    """Write the next line and voice it. Slow (LLM, then TTS), so it runs while the
    previous line is still being spoken. May raise LLMError."""
    # Step 1: choose the speaker and have the model write their line.
    agent = _pick_speaker(sim, history)
    text = await _say(sim, agent, topic, lines)
    if not text:
        return _Turn(agent, "", 0.0)
    duration = max(2.0, len(text.split()) / WORDS_PER_SECOND)

    # A pushy participant may cut in before this line ends. The speaker is
    # recorded only up to the cut plus a short overlap, so the two spans
    # genuinely overlap and the metrics see a real interruption.
    cut: tuple[_Agent, str, float] | None = None
    interrupter = _pick_interrupter(sim, agent, text)
    if interrupter is not None:
        fraction = random.uniform(0.35, 0.75)
        said = _cut(text, fraction)
        try:
            reply = await _say(sim, interrupter, topic, lines, (agent.name, said))
        except LLMError:
            reply = ""
        if reply:
            text = said
            cut = (interrupter, reply, duration * fraction)
            duration = duration * fraction + random.uniform(*OVERLAP_SECONDS)

    # With a voice, the audio's real length sets the timing. A cut line is spoken up
    # to the cut, and the interrupter starts under its last second.
    spoken = await _speak(agent, text.removesuffix(" —"))
    if spoken is not None:
        duration = max(1.0, spoken[1])
    # No interruption: the turn is just this one line.
    if cut is None:
        return _Turn(agent, text, duration, spoken)
    # Interruption: voice the interrupter and fix when they start, so the audio overlaps.
    reply_spoken = await _speak(cut[0], cut[1])
    cut_after = cut[2]
    if spoken is not None:
        overlap = min(random.uniform(*OVERLAP_SECONDS), duration * 0.5)
        cut_after = max(0.5, duration - overlap)
    return _Turn(agent, text, duration, spoken, (cut[0], cut[1], cut_after, reply_spoken))


async def _run(factory: async_sessionmaker[AsyncSession], session_id: uuid.UUID) -> None:
    """Wait for LIVE, then talk until the session leaves it."""
    sim = _simulations[session_id]
    lines: list[tuple[str, str]] = []
    history: list[str] = []
    failures = 0
    waited = 0.0
    # An interrupter's turn, already prepared: (turn, start time).
    pending: tuple[_Turn, float] | None = None
    # The next line being prepared while the current one is spoken.
    prefetch: asyncio.Task[_Turn] | None = None

    try:
        while True:
            # Re-read the session each loop so we react to start/end promptly.
            async with factory() as db:
                gd = await session_service.require_session(db, session_id)
                state, topic = SessionState(gd.state), gd.topic
            # Not live yet: wait in the lobby (up to a cap), or quit if it ended.
            if state in {SessionState.WAITING, SessionState.READY, SessionState.STARTING}:
                if waited > MAX_LOBBY_WAIT_SECONDS:
                    return
                await asyncio.sleep(1)
                waited += 1
                continue
            if state is not SessionState.LIVE:
                return

            # Get the next turn: a queued interruption first, else a prefetched
            # or freshly prepared line. Repeated LLM failures end the loop.
            forced_start: float | None = None
            if pending is not None:
                turn, forced_start = pending
                pending = None
            else:
                try:
                    turn = await (prefetch or _prepare(sim, topic, lines, history))
                    prefetch = None
                    failures = 0
                except LLMError as exc:
                    prefetch = None
                    failures += 1
                    logger.warning(
                        "agent turn failed",
                        extra={"session_id": str(session_id), "error": str(exc)},
                    )
                    if failures >= 5:
                        return
                    await asyncio.sleep(3)
                    continue
            if not turn.text:
                continue
            agent, text, duration, spoken = turn.agent, turn.text, turn.duration, turn.spoken

            # Record the line exactly as real speech would be: a speech event plus
            # a transcript segment, clamped to the session's remaining time.
            async with factory() as db:
                gd = await session_service.require_session(db, session_id)
                if SessionState(gd.state) is not SessionState.LIVE:
                    return
                start = (
                    forced_start
                    if forced_start is not None
                    else session_service.elapsed_discussion_seconds(gd)
                )
                end = min(start + duration, float(gd.duration_seconds))
                await session_service.record_speech(db, gd, agent.label, start, end)
                await speech_service.persist_segment(db, gd.id, agent.label, start, end, text, 0.95)
                await db.commit()

            # Announce the line to the room: started speaking, audio, transcript.
            lines.append((agent.name, text))
            history.append(agent.label)
            speaker = {"candidate_label": agent.label}
            await _broadcast(session_id, ServerEvent.SPEECH_STARTED, speaker)
            if spoken is not None:
                await _broadcast(
                    session_id,
                    ServerEvent.AGENT_AUDIO,
                    {
                        **speaker,
                        "audio_b64": base64.b64encode(spoken[0]).decode("ascii"),
                        "seconds": round(duration, 2),
                    },
                )
            await _broadcast(
                session_id, ServerEvent.TRANSCRIPT_APPENDED, {**speaker, "segments": 1}
            )
            # Interruption path: queue the interrupter's turn to start mid-line.
            if turn.cut_in is not None:
                other, reply, cut_after, reply_spoken = turn.cut_in
                # The interrupter starts mid-sentence; the speaker trails off a
                # moment later. The next loop turn records the interrupter.
                reply_turn = _Turn(other, reply, _duration_of(reply, reply_spoken), reply_spoken)
                pending = (reply_turn, start + cut_after)
                asyncio.create_task(_stop_later(session_id, agent.label, end - start))
                await asyncio.sleep(cut_after)
                continue
            # While this line is spoken, write and voice the next one, so the pause
            # between speakers is the gap and not the time the model takes.
            prefetch = asyncio.create_task(_prepare(sim, topic, lines, history))
            # "Speaking" takes as long as the words would, so the live room and
            # the metrics see a realistic floor.
            await asyncio.sleep(end - start)
            await _broadcast(session_id, ServerEvent.SPEECH_STOPPED, speaker)
            await asyncio.sleep(TURN_GAP_SECONDS)
    except Exception:
        # Contained: agents falling silent must never take the session down.
        logger.exception("agent loop crashed", extra={"session_id": str(session_id)})
    finally:
        # Always clean up: cancel unused prefetch work and forget this simulation.
        if prefetch is not None:
            prefetch.cancel()
        _simulations.pop(session_id, None)


def _duration_of(text: str, spoken: tuple[bytes, float] | None) -> float:
    """How long a line takes to say: the audio length if spoken, else an estimate from the text."""
    # Real audio length when we have it; otherwise estimate from the word count.
    if spoken is not None:
        return max(1.0, spoken[1])
    return max(2.0, len(text.split()) / WORDS_PER_SECOND)
