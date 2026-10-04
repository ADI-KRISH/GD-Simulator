"""The Observer Agent and its loop (design §14–§16, §47).

The invariants that matter here are the ones that are easy to violate by
accident: the Observer must not score, must not replace the transcript, must not
process a window twice, and must not be able to break the live session.
"""

import uuid

import pytest
from httpx import AsyncClient

from services.common.contracts import CandidateMetrics, RollingState, TranscriptSegment
from services.llm import set_llm_provider
from services.llm.base import LLMError, LLMResult
from services.llm.stub_provider import StubLLM
from services.observer import service as observer_service
from services.observer.agent import SYSTEM_PROMPT, build_prompt, observe
from services.session import repository as session_repo
from services.session import service as session_service
from services.speech import repository as speech_repo
from services.speech.chunking import CHUNK_SECONDS, build_chunk
from services.speech.service import persist_segment

SESSION = "GD1"


def seg(label: str, start: float, text: str) -> TranscriptSegment:
    return TranscriptSegment(
        session_id=SESSION,
        candidate_id=label,
        start=start,
        end=start + 5,
        text=text,
        confidence=0.9,
    )


def metrics(label: str, ratio: float) -> CandidateMetrics:
    return CandidateMetrics(
        session_id=SESSION,
        candidate_id=label,
        speaking_seconds=ratio * 100,
        participation_ratio=ratio,
        turns=3,
        interruptions=0,
        responses=1,
        questions=1,
    )


def test_the_system_prompt_forbids_scoring():
    """Design §16 — stated in the prompt as well as enforced by the contract."""
    lowered = SYSTEM_PROMPT.lower()
    assert "do not score" in lowered
    assert "observations, not judgements" in lowered
    assert "not an evaluator" in lowered


def test_the_prompt_carries_all_three_inputs():
    """Design §14.3: previous state + new chunk + latest metrics."""
    previous = RollingState(
        session_id=SESSION,
        main_arguments=["AI increases productivity"],
        candidate_positions={"A": "pro-automation"},
        recent_interactions=[],
        discussion_direction="productivity",
        unresolved_questions=["reskilling feasibility"],
    )
    chunk = build_chunk([seg("B", 50, "That ignores displacement.")], 1)
    prompt = build_prompt(previous, chunk, {"A": metrics("A", 0.6)}, "Should AI be regulated?")

    assert "AI increases productivity" in prompt  # previous state
    assert "That ignores displacement." in prompt  # new chunk
    assert "60% of floor" in prompt  # measured metrics
    assert "Should AI be regulated?" in prompt  # topic


def test_the_prompt_tells_the_model_the_metrics_are_facts():
    """claude.md invariant 4: the model must not re-estimate measured numbers."""
    prompt = build_prompt(
        RollingState.empty(SESSION),
        build_chunk([seg("A", 0, "hello")], 0),
        {"A": metrics("A", 0.5)},
        "topic",
    )
    assert "do not re-estimate" in prompt.lower()


async def test_an_empty_window_is_not_sent_to_the_model():
    """Asking for an update from nothing invites invention."""
    llm = StubLLM()
    set_llm_provider(llm)
    previous = RollingState.empty(SESSION)
    result = await observe(previous, build_chunk([], 0), {}, "topic", SESSION)

    assert result.changed is False
    assert result.state is previous
    assert llm.calls == []


async def test_a_successful_update_returns_validated_state():
    set_llm_provider(StubLLM())
    result = await observe(
        RollingState.empty(SESSION),
        build_chunk([seg("A", 0, "Regulation is overdue.")], 0),
        {"A": metrics("A", 1.0)},
        "topic",
        SESSION,
    )
    assert result.changed is True
    assert result.state.session_id == SESSION


async def test_the_observer_cannot_write_state_onto_another_session():
    """The model is not trusted to echo the session id back."""

    class WrongSessionLLM:
        name = "wrong-session"
        model = "test"

        async def complete_json(self, *, system, prompt, schema, max_tokens=None):
            return LLMResult(
                data={
                    "session_id": "SOMEONE-ELSE",
                    "main_arguments": ["x"],
                    "candidate_positions": {},
                    "recent_interactions": [],
                    "discussion_direction": "y",
                    "unresolved_questions": [],
                },
                model="test",
            )

    set_llm_provider(WrongSessionLLM())
    result = await observe(
        RollingState.empty(SESSION),
        build_chunk([seg("A", 0, "text")], 0),
        {},
        "topic",
        SESSION,
    )
    assert result.state.session_id == SESSION


async def test_a_scoring_observer_response_is_rejected():
    """claude.md invariant 7 — enforced by the contract, not by trust."""

    class ScoringLLM:
        name = "scoring"
        model = "test"

        async def complete_json(self, *, system, prompt, schema, max_tokens=None):
            return LLMResult(
                data={
                    "main_arguments": [],
                    "candidate_positions": {},
                    "recent_interactions": [],
                    "discussion_direction": "x",
                    "unresolved_questions": [],
                    "scores": {"A": 8.5},
                },
                model="test",
            )

    set_llm_provider(ScoringLLM())
    previous = RollingState.empty(SESSION)
    result = await observe(previous, build_chunk([seg("A", 0, "text")], 0), {}, "topic", SESSION)
    assert result.changed is False
    assert result.error is not None
    assert result.state is previous


async def test_an_llm_failure_keeps_the_previous_state():
    """Design §38: the GD continues normally when the LLM fails."""

    class BrokenLLM:
        name = "broken"
        model = "test"

        async def complete_json(self, *, system, prompt, schema, max_tokens=None):
            raise LLMError("provider down")

    set_llm_provider(BrokenLLM())
    previous = RollingState(
        session_id=SESSION,
        main_arguments=["kept"],
        candidate_positions={},
        recent_interactions=[],
        discussion_direction="unchanged",
        unresolved_questions=[],
    )
    result = await observe(previous, build_chunk([seg("A", 0, "text")], 0), {}, "topic", SESSION)
    assert result.state.main_arguments == ["kept"]
    assert result.error == "provider down"


# --------------------------------------------------------------------------
# The loop (design §15)
# --------------------------------------------------------------------------


@pytest.fixture
async def live_with_transcript(
    session_factory, client: AsyncClient, gd_session, host_auth, candidate_auth, second_candidate
):
    """A LIVE session with three closed windows' worth of transcript."""
    session_id = uuid.UUID(gd_session["id"])
    for header in (candidate_auth, second_candidate):
        await client.post("/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=header)
        await client.post(
            f"/api/v1/lobbies/{session_id}/ready", json={"ready": True}, headers=header
        )
    await client.post(f"/api/v1/sessions/{session_id}/start", headers=host_auth)

    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, session_id)
        for index in range(3):
            start = index * CHUNK_SECONDS + 1
            await persist_segment(db, gd.id, "A", start, start + 5, f"point {index}", 0.9)
            await session_service.record_speech(db, gd, "A", start, start + 5)
        await db.commit()
    return session_id


async def test_the_loop_processes_each_closed_window_once(session_factory, live_with_transcript):
    """Design §15: state N+1 comes from state N plus chunk N+1, once."""
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, live_with_transcript)
        first = await observer_service.process_pending_chunks(db, gd, up_to_end=True)
        await db.commit()

    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, live_with_transcript)
        second = await observer_service.process_pending_chunks(db, gd, up_to_end=True)
        await db.commit()
        states = await speech_repo.list_rolling_states(db, live_with_transcript)

    assert first == 3
    assert second == 0, "a processed window must never be processed again"
    assert [s.chunk_index for s in states] == [0, 1, 2]


async def test_the_rolling_state_never_replaces_the_transcript(
    session_factory, live_with_transcript
):
    """claude.md invariant 3 / design §48: both memories survive."""
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, live_with_transcript)
        await observer_service.process_pending_chunks(db, gd, up_to_end=True)
        await db.commit()
        segments = await speech_repo.list_segments(db, live_with_transcript)
        states = await speech_repo.list_rolling_states(db, live_with_transcript)

    assert len(segments) == 3, "raw transcript must be untouched by the Observer"
    assert states


async def test_the_state_starts_empty_and_is_readable_at_any_point(
    session_factory, live_with_transcript
):
    async with session_factory() as db:
        before = await observer_service.current_state(db, live_with_transcript)
        assert before.main_arguments == []
        assert before.discussion_direction == "not yet established"

        gd = await session_repo.get_session_by_id(db, live_with_transcript)
        await observer_service.process_pending_chunks(db, gd, up_to_end=True)
        await db.commit()
        after = await observer_service.current_state(db, live_with_transcript)

    assert after.session_id == str(live_with_transcript)


async def test_a_broken_observer_does_not_stop_the_session(session_factory, live_with_transcript):
    """claude.md invariant 1."""

    class BrokenLLM:
        name = "broken"
        model = "test"

        async def complete_json(self, *, system, prompt, schema, max_tokens=None):
            raise LLMError("down")

    set_llm_provider(BrokenLLM())
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, live_with_transcript)
        updated = await observer_service.process_pending_chunks(db, gd, up_to_end=True)
        await db.commit()

    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, live_with_transcript)
        segments = await speech_repo.list_segments(db, live_with_transcript)

    assert updated == 0
    assert gd.state == "LIVE", "the session must keep running"
    assert len(segments) == 3, "the transcript must be intact"


async def test_state_updates_are_published(session_factory, live_with_transcript, events):
    """Design §36."""
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, live_with_transcript)
        await observer_service.process_pending_chunks(db, gd, up_to_end=True)
        await db.commit()
    assert events.count("state.updated") == 3
