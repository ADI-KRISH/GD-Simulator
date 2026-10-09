"""The group report agent (services/evaluation/group_synthesis.py)."""

from services.common.contracts import CandidateMetrics, RollingState
from services.evaluation.aggregator import AggregatedScore
from services.evaluation.group_synthesis import build_prompt, synthesise_group
from services.llm import StubLLM, set_llm_provider
from services.metrics.engine import SessionMetrics


def _metrics() -> SessionMetrics:
    return SessionMetrics(
        session_id="s1",
        per_candidate={
            "A": CandidateMetrics(
                session_id="s1",
                candidate_id="A",
                speaking_seconds=30.0,
                participation_ratio=0.6,
                turns=3,
                interruptions=2,
                responses=2,
                questions=1,
                arguments_introduced=1,
                arguments_supported=0,
                arguments_challenged=1,
                response_latency_seconds=1.5,
            )
        },
        interactions=[],
        total_speaking_seconds=50.0,
        turn_order=["A"],
    )


def _scores() -> dict[str, AggregatedScore]:
    return {
        "A": AggregatedScore(
            candidate_label="A",
            overall=71.5,
            dimensions={"content": 7.0},
            weights={"content": 1.0},
            confidence=0.8,
            needs_human_review=False,
        )
    }


def test_the_prompt_carries_the_measured_facts_the_agent_may_quote():
    prompt = build_prompt(
        "Remote work", _scores(), {"A": "Arjun"}, _metrics(), RollingState.empty("s1"), {}
    )
    assert "71.5/100" in prompt
    assert "2 interruptions made" in prompt
    assert "Arjun" in prompt


async def test_the_group_agent_returns_a_narrative():
    set_llm_provider(StubLLM())
    try:
        narrative = await synthesise_group(
            "Remote work",
            _scores(),
            {"A": "Arjun"},
            _metrics(),
            RollingState.empty("s1"),
            {},
        )
    finally:
        set_llm_provider(None)
    assert narrative["overview"]
    assert isinstance(narrative["recommendations"], list)


async def test_a_failing_model_costs_only_the_narrative():
    set_llm_provider(StubLLM(fail=True))
    try:
        narrative = await synthesise_group(
            "Remote work",
            _scores(),
            {"A": "Arjun"},
            _metrics(),
            RollingState.empty("s1"),
            {},
        )
    finally:
        set_llm_provider(None)
    assert narrative == {}


async def test_no_scores_means_no_call():
    assert (await synthesise_group("t", {}, {}, _metrics(), RollingState.empty("s1"), {})) == {}
