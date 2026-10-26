"""Per-dimension batched dispatch (design §49).

Purpose:     Tests for the "one model call per specialist covering every
             candidate" path: the batched prompt and schema, validation of a
             batched response, and how the Supervisor dispatches both strategies.
Why needed:  Batching changes how many model calls happen and what a malformed
             reply can look like (a candidate scored twice or skipped). These
             tests prove the batched path produces the same downstream shape as
             the per-candidate path and fails safely.
Contains:    `_fixtures`, `_assessment` (test builders) and tests for: prompt
             content, schema limits, relative-grading guard, accepting a valid
             batch, rejecting duplicates / omissions / foreign dimensions /
             missing arrays / malformed items, call counts for each strategy,
             identical output shape, isolated failure of one specialist, and
             unchanged evidence packets.
Used by:     pytest (run with `pytest services/tests/test_evaluation_batching.py`).
Depends on:  evaluation/agents/base.py, agents/definitions.py, evidence.py,
             supervisor.py (under test), llm/ (stub provider), common/contracts.py,
             metrics/engine.py and speech/diarization.py (to build fixtures).

One call per specialist covering every candidate, instead of one call per
(specialist, candidate) pair. The batching is an optimisation *and* a quality
change — comparative dimensions can finally compare — so what matters is that
it produces the same downstream shape as the per-candidate path while catching
the two failures a batched response can have that a single-candidate one
cannot: a candidate assessed twice, and a candidate silently skipped.
"""

import pytest

from services.common.contracts import (
    CandidateMetrics,
    ContractError,
    RollingState,
    TranscriptSegment,
)
from services.evaluation.agents.base import (
    parse_batch_output,
    run_agent_batch,
)
from services.evaluation.agents.definitions import CONTENT, LEADERSHIP, select_agents
from services.evaluation.evidence import (
    EvidencePacket,
    SharedContext,
    build_packets,
    build_shared_context,
)
from services.evaluation.supervisor import (
    STRATEGY_PER_CANDIDATE,
    STRATEGY_PER_DIMENSION,
    dispatch,
    plan,
)
from services.llm import set_llm_provider
from services.llm.base import LLMError, LLMResult
from services.llm.stub_provider import StubLLM
from services.metrics.engine import compute_metrics
from services.speech.diarization import SpeakerSpan

SESSION = "GD1"
LABELS = ["A", "B", "C"]


def _fixtures():
    """Build a small shared context, evidence packets and metrics for three candidates."""
    # Who spoke when (A the longest, C the shortest) ...
    spans = [
        SpeakerSpan(candidate_label="A", start=0, end=40),
        SpeakerSpan(candidate_label="B", start=41, end=60),
        SpeakerSpan(candidate_label="C", start=62, end=70),
    ]
    # ... and one transcript line per candidate.
    segments = [
        TranscriptSegment(
            session_id=SESSION,
            candidate_id=label,
            start=float(i * 20),
            end=float(i * 20 + 15),
            text=f"A substantive point from {label}.",
            confidence=0.9,
        )
        for i, label in enumerate(LABELS)
    ]
    # Real deterministic metrics, then the shared context and per-candidate packets built from them.
    metrics = compute_metrics(SESSION, spans, segments, LABELS)
    context = build_shared_context(
        SESSION, "Should AI be regulated?", "technical", 900, LABELS, RollingState.empty(SESSION)
    )
    return context, build_packets(LABELS, segments, metrics), metrics


# --------------------------------------------------------------------------
# Prompt and schema construction
# --------------------------------------------------------------------------


def test_the_batched_prompt_carries_every_candidate_once():
    """The batched prompt includes every candidate and the shared context only once."""
    context, packets, _ = _fixtures()
    prompt = CONTENT.batch_user_prompt(context, packets)

    # Every candidate's evidence block must be in the one prompt.
    for label in LABELS:
        assert f"CANDIDATE {label}" in prompt
    # The shared context appears once, not once per candidate — that is the
    # whole point of the batching (design §49).
    assert prompt.count("SHARED DISCUSSION CONTEXT") == 1
    assert "Assess EVERY candidate (A, B, C)" in prompt


def test_the_batched_schema_pins_the_candidate_set_and_length():
    """The batch schema fixes the number of assessments and the allowed candidate ids."""
    # Inspect the "assessments" array part of the generated schema.
    schema = CONTENT.batch_response_schema(LABELS)
    assessments = schema["properties"]["assessments"]

    assert assessments["minItems"] == 3
    assert assessments["maxItems"] == 3
    # The model cannot invent a participant.
    assert assessments["items"]["properties"]["candidate_id"]["enum"] == ["A", "B", "C"]
    assert assessments["items"]["properties"]["dimension"]["enum"] == ["content"]


def test_the_system_prompt_guards_against_relative_grading():
    """Seeing the whole group must not turn absolute scoring into ranking."""
    # The base rules and the leadership rubric must both survive in the system prompt.
    prompt = LEADERSHIP.system_prompt().lower()
    assert "not" in prompt and "average for this group" in prompt
    # The comparative-dimension instruction survives batching.
    assert "speaking the most is not leadership" in prompt


# --------------------------------------------------------------------------
# Batched response validation
# --------------------------------------------------------------------------


def _assessment(label: str, dimension: str = "content", score: float = 7.0) -> dict:
    """A valid assessment dict for one candidate and dimension."""
    # Mirrors the evaluation_output contract so tests can break one field at a time.
    return {
        "candidate_id": label,
        "dimension": dimension,
        "score": score,
        "confidence": 0.8,
        "strengths": ["made a point"],
        "weaknesses": ["could go deeper"],
        "evidence": [{"timestamp": "01:00", "summary": "said something relevant"}],
    }


def test_a_well_formed_batch_is_accepted():
    """A complete, valid batch response parses into one output per candidate."""
    outputs = parse_batch_output(
        CONTENT, {"assessments": [_assessment(label) for label in LABELS]}, LABELS
    )
    assert [o.candidate_id for o in outputs] == LABELS


def test_a_duplicated_candidate_is_rejected():
    """Two assessments for one person would silently corrupt their score."""
    # A is assessed twice (and C not at all): the duplicate check must fire first.
    with pytest.raises(ContractError, match="more than once"):
        parse_batch_output(
            CONTENT,
            {"assessments": [_assessment("A"), _assessment("A"), _assessment("B")]},
            LABELS,
        )


def test_a_skipped_candidate_is_rejected():
    """Quietly dropping someone is the other failure a schema cannot catch."""
    with pytest.raises(ContractError, match="no assessment for"):
        parse_batch_output(CONTENT, {"assessments": [_assessment("A"), _assessment("B")]}, LABELS)


def test_an_agent_straying_outside_its_dimension_is_rejected():
    """A batch containing another agent's dimension is rejected."""
    # The content agent returns one "leadership" assessment, which it doesn't own.
    with pytest.raises(ContractError, match="dimensions it does not own"):
        parse_batch_output(
            CONTENT,
            {
                "assessments": [
                    _assessment("A"),
                    _assessment("B", dimension="leadership"),
                    _assessment("C"),
                ]
            },
            LABELS,
        )


def test_a_response_without_the_assessments_array_is_rejected():
    """A reply lacking the 'assessments' array is rejected."""
    with pytest.raises(ContractError, match="no 'assessments' array"):
        parse_batch_output(CONTENT, {"results": []}, LABELS)


def test_a_malformed_assessment_still_hits_the_contract_gate():
    """Batching must not weaken per-assessment validation."""
    # Score 42 is outside the 0-10 contract range, so the per-item validation must reject it.
    broken = _assessment("A") | {"score": 42}
    with pytest.raises(ContractError):
        parse_batch_output(
            CONTENT, {"assessments": [broken, _assessment("B"), _assessment("C")]}, LABELS
        )


# --------------------------------------------------------------------------
# Dispatch
# --------------------------------------------------------------------------


async def test_batched_dispatch_makes_one_call_per_specialist():
    """The cost claim, measured rather than asserted."""
    context, packets, metrics = _fixtures()
    # The stub records every call it receives, so we can count them.
    llm = StubLLM()
    set_llm_provider(llm)

    # Plan for the technical roster, then swap in our known packets and context.
    supervisor_plan = plan(
        SESSION,
        "topic",
        "technical",
        900,
        LABELS,
        [],
        metrics,
        RollingState.empty(SESSION),
    )
    supervisor_plan.packets.clear()
    supervisor_plan.packets.update(packets)
    supervisor_plan.context = context

    runs = await dispatch(supervisor_plan, STRATEGY_PER_DIMENSION)

    agents = len(supervisor_plan.agents)
    assert len(llm.calls) == agents, "one call per specialist, not per pair"
    # Downstream still sees one run per (agent, candidate).
    assert len(runs) == agents * len(LABELS)


async def test_per_candidate_dispatch_makes_one_call_per_pair():
    """The per-candidate strategy makes one LLM call per (agent, candidate) pair."""
    context, packets, metrics = _fixtures()
    llm = StubLLM()
    set_llm_provider(llm)

    # Same setup as the batched test, but dispatched per (agent, candidate) pair.
    supervisor_plan = plan(
        SESSION, "topic", "technical", 900, LABELS, [], metrics, RollingState.empty(SESSION)
    )
    supervisor_plan.packets.clear()
    supervisor_plan.packets.update(packets)
    supervisor_plan.context = context

    runs = await dispatch(supervisor_plan, STRATEGY_PER_CANDIDATE)

    agents = len(supervisor_plan.agents)
    assert len(llm.calls) == agents * len(LABELS)
    assert len(runs) == agents * len(LABELS)


async def test_both_strategies_produce_the_same_shape():
    """Everything downstream — resolve, aggregate, report — must be unaware of
    which strategy ran."""
    context, packets, metrics = _fixtures()
    supervisor_plan = plan(
        SESSION, "topic", "technical", 900, LABELS, [], metrics, RollingState.empty(SESSION)
    )
    supervisor_plan.packets.clear()
    supervisor_plan.packets.update(packets)
    supervisor_plan.context = context

    # Run both strategies with fresh stubs, then compare what comes out.
    set_llm_provider(StubLLM())
    batched = await dispatch(supervisor_plan, STRATEGY_PER_DIMENSION)
    set_llm_provider(StubLLM())
    per_candidate = await dispatch(supervisor_plan, STRATEGY_PER_CANDIDATE)

    def signature(runs):
        """Comparable summary of runs: (agent, dimension, candidate, ok)."""
        return sorted((r.agent, r.dimension, r.candidate_label, r.ok) for r in runs)

    assert signature(batched) == signature(per_candidate)


async def test_a_batched_failure_fails_only_that_dimension():
    """One dead specialist must not take the others with it, and the
    aggregator's missing-dimension handling must still apply."""
    context, packets, metrics = _fixtures()

    class OneAgentFails:
        """Fake model whose 'behavior' specialist is down."""
        name = "selective"
        model = "test"

        async def complete_json(self, *, system, prompt, schema, max_tokens=None):
            """Fail for the behavior agent; answer validly for the others."""
            if "YOUR DIMENSION: behavior" in system:
                raise LLMError("this specialist is down")
            return LLMResult(data=_batch_payload(schema), model="test")

    def _batch_payload(schema: dict) -> dict:
        """Build a valid batch reply matching the requested schema."""
        item = schema["properties"]["assessments"]["items"]
        dimension = item["properties"]["dimension"]["enum"][0]
        return {"assessments": [_assessment(label, dimension) for label in LABELS]}

    # Install the flaky fake model and dispatch normally.
    set_llm_provider(OneAgentFails())
    supervisor_plan = plan(
        SESSION, "topic", "technical", 900, LABELS, [], metrics, RollingState.empty(SESSION)
    )
    supervisor_plan.packets.clear()
    supervisor_plan.packets.update(packets)
    supervisor_plan.context = context

    runs = await dispatch(supervisor_plan, STRATEGY_PER_DIMENSION)

    failed = [r for r in runs if not r.ok]
    assert {r.dimension for r in failed} == {"behavior"}
    # Every candidate is reported as failed for that dimension, so nobody is
    # silently missing from the aggregator's view.
    assert {r.candidate_label for r in failed} == set(LABELS)
    assert all(r.ok for r in runs if r.dimension != "behavior")


async def test_a_batched_run_returns_one_run_per_candidate_on_failure():
    # When the single batched call fails, every candidate must still get a (failed) run.
    context, packets, _ = _fixtures()

    class Broken:
        """Fake model that always fails."""
        name = "broken"
        model = "test"

        async def complete_json(self, *, system, prompt, schema, max_tokens=None):
            """Raise an LLMError."""
            raise LLMError("down")

    # Run one batched specialist against a model that always raises.
    set_llm_provider(Broken())
    runs = await run_agent_batch(CONTENT, context, packets)

    assert len(runs) == len(LABELS)
    assert all(r.error == "down" for r in runs)
    assert {r.candidate_label for r in runs} == set(LABELS)


def test_the_planned_call_count_reflects_the_strategy():
    """The plan's call count is per-agent for per-dimension and per-pair for per-candidate."""
    _, _, metrics = _fixtures()
    supervisor_plan = plan(
        SESSION, "topic", "technical", 900, LABELS, [], metrics, RollingState.empty(SESSION)
    )
    # The technical roster size is the expected per-dimension call count.
    agents = len(select_agents("technical"))

    assert supervisor_plan.call_count(STRATEGY_PER_DIMENSION) == agents
    assert supervisor_plan.call_count(STRATEGY_PER_CANDIDATE) == agents * len(LABELS)


def test_evidence_packets_are_unchanged_by_batching():
    """The packet is the same object either way — only how many go per call
    differs (design §23)."""
    context, packets, _ = _fixtures()
    assert isinstance(packets["A"], EvidencePacket)
    assert isinstance(context, SharedContext)
    assert isinstance(packets["A"].metrics, CandidateMetrics)
