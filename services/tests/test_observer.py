"""The Observer Agent and its loop (design §14–§16, §47).

The invariants that matter here are the ones that are easy to violate by
accident: the Observer must not score, must not replace the transcript, must not
process a window twice, and must not be able to break the live session.

Purpose: Tests for the Observer agent and its loop: prompt contents, empty windows, validated
    output, contract enforcement, failure handling, one-pass processing and what gets published.
Why needed: The Observer invariants are easy to break by accident: it must not score, must not
    replace the transcript, must not process a window twice and must never be able to break a
    live session. These tests pin each one down.
Contains:
  - seg / metrics: helpers that build transcript segments and candidate metrics.
  - Prompt tests: it forbids scoring, carries previous state, chunk, metrics and topic, and
    tells the model metrics are facts.
  - observe tests: empty windows skip the model, good replies become validated state, a wrong
    session id is overridden, a scoring reply is rejected, an LLM failure keeps the previous
    state.
  - live_with_transcript: fixture with a LIVE session and three windows of transcript.
  - Loop tests: each window processed once, committed before the next model call, transcript
    untouched, empty starting state, broken Observer does not stop the session, events and ideas
    are published.
Used by: nothing imports it directly (run by pytest, which discovers test_*.py files)
Depends on: services/common/contracts.py, services/llm/__init__.py, services/llm/base.py,
    services/llm/stub_provider.py, services/observer/__init__.py, services/observer/agent.py,
    services/session/__init__.py, services/speech/__init__.py, services/speech/chunking.py,
    services/speech/service.py
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
    """Build a 5-second transcript segment."""
    return TranscriptSegment(
        session_id=SESSION,
        candidate_id=label,
        start=start,
        end=start + 5,
        text=text,
        confidence=0.9,
    )


def metrics(label: str, ratio: float) -> CandidateMetrics:
    """Build metrics for a candidate with the given participation ratio."""
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
    # Case-insensitive phrase checks on the prompt.
    lowered = SYSTEM_PROMPT.lower()
    assert "do not score" in lowered
    assert "observations, not judgements" in lowered
    assert "not an evaluator" in lowered


def test_the_prompt_carries_all_three_inputs():
    """Design §14.3: previous state + new chunk + latest metrics."""
    # A non-empty previous state that must appear in the prompt.
    previous = RollingState(
        session_id=SESSION,
        main_arguments=["AI increases productivity"],
        candidate_positions={"A": "pro-automation"},
        recent_interactions=[],
        discussion_direction="productivity",
        unresolved_questions=["reskilling feasibility"],
    )
    # A window holding one new line of speech.
    chunk = build_chunk([seg("B", 50, "That ignores displacement.")], 1)
    # Assemble the prompt from all four inputs.
    prompt = build_prompt(previous, chunk, {"A": metrics("A", 0.6)}, "Should AI be regulated?")

    assert "AI increases productivity" in prompt  # previous state
    assert "That ignores displacement." in prompt  # new chunk
    assert "60% of floor" in prompt  # measured metrics
    assert "Should AI be regulated?" in prompt  # topic


def test_the_prompt_tells_the_model_the_metrics_are_facts():
    """claude.md invariant 4: the model must not re-estimate measured numbers."""
    # Minimal inputs: only the wording about metrics matters here.
    prompt = build_prompt(
        RollingState.empty(SESSION),
        build_chunk([seg("A", 0, "hello")], 0),
        {"A": metrics("A", 0.5)},
        "topic",
    )
    assert "do not re-estimate" in prompt.lower()


async def test_an_empty_window_is_not_sent_to_the_model():
    """Asking for an update from nothing invites invention."""
    # The stub provider records every call, so it works as a spy.
    llm = StubLLM()
    set_llm_provider(llm)
    previous = RollingState.empty(SESSION)
    # Observe an empty chunk.
    result = await observe(previous, build_chunk([], 0), {}, "topic", SESSION)

    # Nothing changes and the very same state object comes back...
    assert result.changed is False
    assert result.state is previous
    # ...and the model was never called.
    assert llm.calls == []


async def test_a_successful_update_returns_validated_state():
    """A good model reply becomes a validated, changed state."""
    # Use the deterministic offline model.
    set_llm_provider(StubLLM())
    # Fold one chunk of speech into an empty state.
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

    # A fake model that returns a state naming a different session.
    class WrongSessionLLM:
        """Fake model that claims a different session id."""
        name = "wrong-session"
        model = "test"

        async def complete_json(self, *, system, prompt, schema, max_tokens=None):
            """Return a state naming another session."""
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

    # Install the fake model.
    set_llm_provider(WrongSessionLLM())
    result = await observe(
        RollingState.empty(SESSION),
        build_chunk([seg("A", 0, "text")], 0),
        {},
        "topic",
        SESSION,
    )
    # The real session id wins; the model's claim is ignored.
    assert result.state.session_id == SESSION


async def test_a_scoring_observer_response_is_rejected():
    """claude.md invariant 7 — enforced by the contract, not by trust."""

    # A fake model that tries to smuggle scores into its output.
    class ScoringLLM:
        """Fake model that sneaks scores into its output."""
        name = "scoring"
        model = "test"

        async def complete_json(self, *, system, prompt, schema, max_tokens=None):
            """Return a state containing score fields."""
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
    # The contract has no scores field, so this response must be rejected.
    result = await observe(previous, build_chunk([seg("A", 0, "text")], 0), {}, "topic", SESSION)
    # Rejected: the state is unchanged, an error is recorded and the previous state is returned.
    assert result.changed is False
    assert result.error is not None
    assert result.state is previous


async def test_an_llm_failure_keeps_the_previous_state():
    """Design §38: the GD continues normally when the LLM fails."""

    # A fake model that is down.
    class BrokenLLM:
        """Fake model that is down."""
        name = "broken"
        model = "test"

        async def complete_json(self, *, system, prompt, schema, max_tokens=None):
            """Raise an LLMError."""
            raise LLMError("provider down")

    set_llm_provider(BrokenLLM())
    # A state with content that must survive the failure.
    previous = RollingState(
        session_id=SESSION,
        main_arguments=["kept"],
        candidate_positions={},
        recent_interactions=[],
        discussion_direction="unchanged",
        unresolved_questions=[],
    )
    # The provider raises LLMError inside observe.
    result = await observe(previous, build_chunk([seg("A", 0, "text")], 0), {}, "topic", SESSION)
    # The old state is kept and the error is reported rather than raised.
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
    # Two candidates join and ready up; the host then starts the session.
    for header in (candidate_auth, second_candidate):
        await client.post("/api/v1/lobbies/join", json={"code": gd_session["code"]}, headers=header)
        await client.post(
            f"/api/v1/lobbies/{session_id}/ready", json={"ready": True}, headers=header
        )
    await client.post(f"/api/v1/sessions/{session_id}/start", headers=host_auth)

    # Add speech covering three consecutive 45-second windows.
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, session_id)
        # One segment per window, offset by a second so it falls inside its window.
        for index in range(3):
            start = index * CHUNK_SECONDS + 1
            # A transcript row plus the matching speech event.
            await persist_segment(db, gd.id, "A", start, start + 5, f"point {index}", 0.9)
            await session_service.record_speech(db, gd, "A", start, start + 5)
        await db.commit()
    return session_id


async def test_the_loop_processes_each_closed_window_once(session_factory, live_with_transcript):
    """Design §15: state N+1 comes from state N plus chunk N+1, once."""
    # The first pass processes all three windows.
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, live_with_transcript)
        first = await observer_service.process_pending_chunks(db, gd, up_to_end=True)
        await db.commit()

    # The second pass must find nothing left to do.
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, live_with_transcript)
        second = await observer_service.process_pending_chunks(db, gd, up_to_end=True)
        await db.commit()
        states = await speech_repo.list_rolling_states(db, live_with_transcript)

    assert first == 3
    assert second == 0, "a processed window must never be processed again"
    # One stored state per window, in order.
    assert [s.chunk_index for s in states] == [0, 1, 2]


async def test_each_window_is_committed_before_the_next_model_call(
    session_factory, live_with_transcript, monkeypatch
):
    """A write held across a model call locked every other SQLite writer.

    On a rate-limited free tier one observation can take minutes; with the
    windows folded in one transaction, registration answered 500 meanwhile
    (2026-10-17). Each window's state is committed before the next call.
    """
    # Record the order of observe and commit calls.
    events: list[str] = []
    # Wrap the real functions to log when they run.
    real_observe = observer_service.observe

    async def watched_observe(*args, **kwargs):
        """Wrap observe() to log when each model call happens."""
        events.append("observe")
        return await real_observe(*args, **kwargs)

    # Swap in the logging wrapper.
    monkeypatch.setattr(observer_service, "observe", watched_observe)
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, live_with_transcript)
        real_commit = db.commit

        async def watched_commit():
            events.append("commit")
            await real_commit()

        # The same for commit.
        monkeypatch.setattr(db, "commit", watched_commit)
        await observer_service.process_pending_chunks(db, gd, up_to_end=True)

    # Three model calls...
    assert events.count("observe") == 3
    # ...never two in a row without a commit between them.
    assert "observe,observe" not in ",".join(events)


async def test_the_rolling_state_never_replaces_the_transcript(
    session_factory, live_with_transcript
):
    """claude.md invariant 3 / design §48: both memories survive."""
    # Run the Observer over all windows.
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, live_with_transcript)
        await observer_service.process_pending_chunks(db, gd, up_to_end=True)
        await db.commit()
        # Read both memories back: transcript and rolling states.
        segments = await speech_repo.list_segments(db, live_with_transcript)
        states = await speech_repo.list_rolling_states(db, live_with_transcript)

    assert len(segments) == 3, "raw transcript must be untouched by the Observer"
    assert states


async def test_the_state_starts_empty_and_is_readable_at_any_point(
    session_factory, live_with_transcript
):
    """Before any update the state is the empty starting state."""
    # Check the state before and after processing.
    async with session_factory() as db:
        before = await observer_service.current_state(db, live_with_transcript)
        assert before.main_arguments == []
        # The documented starting value.
        assert before.discussion_direction == "not yet established"

        gd = await session_repo.get_session_by_id(db, live_with_transcript)
        await observer_service.process_pending_chunks(db, gd, up_to_end=True)
        await db.commit()
        after = await observer_service.current_state(db, live_with_transcript)

    # After processing, the state belongs to this session.
    assert after.session_id == str(live_with_transcript)


async def test_a_broken_observer_does_not_stop_the_session(session_factory, live_with_transcript):
    """claude.md invariant 1."""

    # A fake model that is down.
    class BrokenLLM:
        """Fake model that is down."""
        name = "broken"
        model = "test"

        async def complete_json(self, *, system, prompt, schema, max_tokens=None):
            """Raise an LLMError."""
            raise LLMError("down")

    # Every Observer step will fail.
    set_llm_provider(BrokenLLM())
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, live_with_transcript)
        # The loop must not raise.
        updated = await observer_service.process_pending_chunks(db, gd, up_to_end=True)
        await db.commit()

    # Reload the session and transcript from the database.
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, live_with_transcript)
        segments = await speech_repo.list_segments(db, live_with_transcript)

    # No state changed...
    assert updated == 0
    assert gd.state == "LIVE", "the session must keep running"
    assert len(segments) == 3, "the transcript must be intact"


async def test_state_updates_are_published(session_factory, live_with_transcript, events):
    """Design §36."""
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, live_with_transcript)
        await observer_service.process_pending_chunks(db, gd, up_to_end=True)
        await db.commit()
    # One event per processed window.
    assert events.count("state.updated") == 3


async def test_the_room_hears_the_latest_ideas(session_factory, live_with_transcript, monkeypatch):
    """M14 FR-14.13: the "recent ideas" strip comes from the Observer's own state."""
    # Capture what would be sent to the room.
    frames: list[dict] = []

    async def capture(session_id, frame):
        """Collect broadcast frames."""
        frames.append(frame)

    # Replace broadcast with the capture function.
    monkeypatch.setattr(observer_service.hub, "broadcast", capture)
    async with session_factory() as db:
        gd = await session_repo.get_session_by_id(db, live_with_transcript)
        await observer_service.process_pending_chunks(db, gd, up_to_end=True)

    # Keep only the ideas frames.
    ideas = [f for f in frames if f["type"] == "IDEAS_UPDATED"]
    assert ideas, "each changed state reaches the room"
    # The frame carries ideas and open questions...
    assert set(ideas[-1]["payload"]) >= {"ideas", "open_questions"}
    # ...limited to the most recent few.
    assert len(ideas[-1]["payload"]["ideas"]) <= observer_service.RECENT_IDEAS
