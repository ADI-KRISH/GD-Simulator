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
from services.realtime.hub import hub
from services.realtime.protocol import ServerEvent, server_frame
from services.session import service as session_service
from services.session.schemas import LobbyView
from services.session.state import SessionState
from services.speech import service as speech_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/v1/sessions", tags=["simulation"])

WORDS_PER_SECOND = 2.6
# Gap between one agent finishing and the next starting, on top of the LLM call.
TURN_GAP_SECONDS = 1.0
# How long a configured-but-unstarted lobby keeps its agents waiting.
MAX_LOBBY_WAIT_SECONDS = 2 * 60 * 60
TALKATIVENESS = {"low": 1.0, "medium": 2.0, "high": 4.0}

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


class AgentsRequest(BaseModel):
    agents: list[AgentSpec] = Field(min_length=1, max_length=8)
    extra_prompt: str = Field(default="", max_length=2000)


@dataclass(slots=True)
class _Agent:
    label: str
    name: str
    persona: str
    weight: float


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


async def _say(sim: _Simulation, agent: _Agent, topic: str, lines: list[tuple[str, str]]) -> str:
    others = ", ".join(a.name for a in sim.agents.values() if a.label != agent.label)
    history = "\n".join(f"{who}: {text}" for who, text in lines[-20:]) or "(nobody has spoken yet)"
    system = (
        f"You are {agent.name}, a candidate in a live group discussion with {others}.\n"
        f"Topic: {topic}\n\nYour persona:\n{agent.persona}\n"
        + (f"\nAdditional instructions:\n{sim.extra_prompt}\n" if sim.extra_prompt else "")
        + "\nSpeak naturally, as spoken words — no lists, no markdown, no stage directions, "
        'no name prefix. Return JSON: {"text": "<what you say next>"}.'
    )
    result = await get_llm_provider().complete_json(
        system=system,
        prompt=f"Discussion so far:\n{history}\n\nWhat do you say next?",
        schema=LINE_SCHEMA,
        max_tokens=400,
    )
    return str(result.data.get("text", "")).strip().strip('"')


async def _broadcast(session_id: uuid.UUID, event: ServerEvent, payload: dict) -> None:
    await hub.broadcast(str(session_id), server_frame(event, str(session_id), payload))


async def _run(factory: async_sessionmaker[AsyncSession], session_id: uuid.UUID) -> None:
    """Wait for LIVE, then talk until the session leaves it."""
    sim = _simulations[session_id]
    lines: list[tuple[str, str]] = []
    history: list[str] = []
    failures = 0
    waited = 0.0

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

            agent = _pick_speaker(sim, history)
            try:
                text = await _say(sim, agent, topic, lines)
                failures = 0
            except LLMError as exc:
                failures += 1
                logger.warning(
                    "agent turn failed", extra={"session_id": str(session_id), "error": str(exc)}
                )
                if failures >= 5:
                    return
                await asyncio.sleep(3)
                continue
            if not text:
                continue

            duration = max(2.0, len(text.split()) / WORDS_PER_SECOND)
            async with factory() as db:
                gd = await session_service.require_session(db, session_id)
                if SessionState(gd.state) is not SessionState.LIVE:
                    return
                start = session_service.elapsed_discussion_seconds(gd)
                end = min(start + duration, float(gd.duration_seconds))
                await session_service.record_speech(db, gd, agent.label, start, end)
                await speech_service.persist_segment(db, gd.id, agent.label, start, end, text, 0.95)
                await db.commit()

            lines.append((agent.name, text))
            history.append(agent.label)
            speaker = {"candidate_label": agent.label}
            await _broadcast(session_id, ServerEvent.SPEECH_STARTED, speaker)
            await _broadcast(
                session_id, ServerEvent.TRANSCRIPT_APPENDED, {**speaker, "segments": 1}
            )
            # "Speaking" takes as long as the words would, so the live room and
            # the metrics see a realistic floor.
            await asyncio.sleep(end - start)
            await _broadcast(session_id, ServerEvent.SPEECH_STOPPED, speaker)
            await asyncio.sleep(TURN_GAP_SECONDS)
    except Exception:
        # Contained: agents falling silent must never take the session down.
        logger.exception("agent loop crashed", extra={"session_id": str(session_id)})
    finally:
        _simulations.pop(session_id, None)
