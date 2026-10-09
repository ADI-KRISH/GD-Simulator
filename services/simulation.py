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
"""

import asyncio
import base64
import logging
import random
import secrets
import uuid
from dataclasses import dataclass, field
from typing import Annotated

from fastapi import APIRouter, Depends
from pydantic import BaseModel, Field
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from services.auth.deps import CurrentUser, SessionDep
from services.auth.models import Profile, Role, User
from services.auth.repository import create_user
from services.common.config import settings
from services.common.db import get_session_factory
from services.common.errors import ConflictError
from services.common.security import hash_password
from services.llm import LLMError, get_llm_provider
from services.practice.personas import PRESETS
from services.realtime.hub import hub
from services.realtime.protocol import ServerEvent, server_frame
from services.session import service as session_service
from services.session.schemas import LobbyView
from services.session.state import SessionState
from services.speech import service as speech_service
from services.speech.tts import TTSError, get_tts_provider

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
    name: str = Field(min_length=1, max_length=60)
    persona: str = Field(min_length=1, max_length=2000)
    talkativeness: str = Field(default="medium", pattern="^(low|medium|high)$")
    # How readily they cut in on someone who is still speaking.
    interrupts: str = Field(default="medium", pattern="^(low|medium|high)$")


class AgentsRequest(BaseModel):
    agents: list[AgentSpec] = Field(min_length=1, max_length=8)
    extra_prompt: str = Field(default="", max_length=2000)


@dataclass(slots=True)
class _Agent:
    label: str
    name: str
    persona: str
    weight: float
    interrupt_p: float = 0.15
    voice_id: str = ""


@dataclass(slots=True)
class _Simulation:
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
    if settings.app_env != "local":
        raise ConflictError("AI agents are only available in the local environment")
    gd = await session_service.require_session(db, session_id)
    session_service.require_host(gd, user)

    sim = _simulations.setdefault(session_id, _Simulation())
    sim.extra_prompt = payload.extra_prompt.strip()
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

    view = await session_service.lobby_view(db, gd)
    await db.commit()
    await hub.broadcast(
        str(gd.id),
        server_frame(ServerEvent.USER_JOINED, str(gd.id), {"occupancy": view.occupancy}),
    )
    if sim.task is None or sim.task.done():
        sim.task = asyncio.create_task(_run(factory, session_id), name=f"agents-{session_id}")
    return view


def _voice_for(name: str, taken: list[str]) -> str:
    """An agent named after a preset persona keeps that persona's voice; the rest get
    the next unused voice from the pool so no two agents share one."""
    preset = PRESETS.get(name.strip().lower())
    if preset is not None and preset.voice_id not in taken:
        return preset.voice_id
    for key in VOICE_POOL:
        if PRESETS[key].voice_id not in taken:
            return PRESETS[key].voice_id
    return PRESETS[VOICE_POOL[len(taken) % len(VOICE_POOL)]].voice_id


async def _speak(agent: _Agent, text: str) -> tuple[bytes, float] | None:
    """The line as spoken audio and its true length, or None to fall back to text only."""
    tts = get_tts_provider()
    if not settings.sim_voices or not tts.produces_audio or not agent.voice_id:
        return None
    try:
        audio = b"".join([chunk async for chunk in tts.stream(text, agent.voice_id)])
    except TTSError as exc:
        logger.warning("agent voice failed", extra={"agent": agent.name, "error": str(exc)})
        return None
    if not audio:
        return None
    return audio, len(audio) / tts.mp3_bytes_per_second


def _pick_speaker(sim: _Simulation, history: list[str]) -> _Agent:
    agents = list(sim.agents.values())
    # Talkativeness skews the floor, but nobody goes silent for long: a silent
    # agent would score zero everywhere and test nothing.
    window = 2 * len(agents)
    for agent in agents:
        if len(history) >= window and agent.label not in history[-window:]:
            return agent
    candidates = [a for a in agents if not history or a.label != history[-1]] or agents
    return random.choices(candidates, weights=[a.weight for a in candidates])[0]


async def _say(
    sim: _Simulation,
    agent: _Agent,
    topic: str,
    lines: list[tuple[str, str]],
    cutting_in_on: tuple[str, str] | None = None,
) -> str:
    others = ", ".join(a.name for a in sim.agents.values() if a.label != agent.label)
    history = "\n".join(f"{who}: {text}" for who, text in lines[-20:]) or "(nobody has spoken yet)"
    system = (
        f"You are {agent.name}, a candidate in a live group discussion with {others}.\n"
        f"Topic: {topic}\n\nYour persona:\n{agent.persona}\n"
        + (f"\nAdditional instructions:\n{sim.extra_prompt}\n" if sim.extra_prompt else "")
        + "\nThis is a real, live group discussion, so behave like a real participant: respond "
        "directly to what was just said, by name when you agree or disagree, build on or "
        "challenge a specific point, and stay on the topic without repeating what has been "
        "said. Speak in a few sentences at most, as spoken words — no lists, no markdown, no "
        'stage directions, no name prefix. Return JSON: {"text": "<what you say next>"}.'
    )
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
    result = await get_llm_provider().complete_json(
        system=system,
        prompt=prompt,
        schema=LINE_SCHEMA,
        max_tokens=400,
    )
    return str(result.data.get("text", "")).strip().strip('"')


def _pick_interrupter(sim: _Simulation, speaker: _Agent, text: str) -> _Agent | None:
    """Someone else may cut in on a long enough line, more likely if they are pushy."""
    if len(text.split()) < MIN_WORDS_TO_INTERRUPT:
        return None
    for other in random.sample(list(sim.agents.values()), len(sim.agents)):
        if other.label != speaker.label and random.random() < other.interrupt_p:
            return other
    return None


def _cut(text: str, fraction: float) -> str:
    """What the speaker had said by the time they were cut in on."""
    words = text.split()
    kept = max(3, int(len(words) * fraction))
    return " ".join(words[:kept]).rstrip(",;:") + " —"


async def _stop_later(session_id: uuid.UUID, label: str, delay: float) -> None:
    await asyncio.sleep(delay)
    await _broadcast(session_id, ServerEvent.SPEECH_STOPPED, {"candidate_label": label})


async def _broadcast(session_id: uuid.UUID, event: ServerEvent, payload: dict) -> None:
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
    if cut is None:
        return _Turn(agent, text, duration, spoken)
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
            async with factory() as db:
                gd = await session_service.require_session(db, session_id)
                state, topic = SessionState(gd.state), gd.topic
            if state in {SessionState.WAITING, SessionState.READY, SessionState.STARTING}:
                if waited > MAX_LOBBY_WAIT_SECONDS:
                    return
                await asyncio.sleep(1)
                waited += 1
                continue
            if state is not SessionState.LIVE:
                return

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
        if prefetch is not None:
            prefetch.cancel()
        _simulations.pop(session_id, None)


def _duration_of(text: str, spoken: tuple[bytes, float] | None) -> float:
    if spoken is not None:
        return max(1.0, spoken[1])
    return max(2.0, len(text.split()) / WORDS_PER_SECOND)
