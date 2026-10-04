"""The evaluation workflow as a LangGraph state graph (design §20, §21).

What these defend is that adopting a framework did not move the boundaries the
system is built on: the aggregate node still runs no model, the contracts still
gate every agent response, and a graph run produces the same result shape as the
native engine — so the two are comparable on the same session.

Also covered: the reducer/checkpointer interaction that duplicated every
candidate the first time this ran. `runs` uses `operator.add`, so resuming a
thread appends rather than replaces; each attempt therefore needs its own
thread id.
"""

import pytest

from services.common.contracts import RollingState, TranscriptSegment
from services.evaluation import graph as graph_module
from services.evaluation.agents.definitions import select_agents
from services.evaluation.aggregator import aggregate_candidate
from services.evaluation.graph import (
    EvaluationState,
    build_graph,
    render_mermaid,
    run_graph,
)
from services.llm import set_llm_provider
from services.llm.base import LLMError
from services.llm.stub_provider import StubLLM
from services.metrics.engine import compute_metrics
from services.session.models import DEFAULT_WEIGHTS
from services.speech.diarization import SpeakerSpan

SESSION = "GD-graph"
LABELS = ["A", "B", "C"]
WEIGHTS = dict(DEFAULT_WEIGHTS)


def _fixtures():
    spans = [
        SpeakerSpan(candidate_label="A", start=0, end=45),
        SpeakerSpan(candidate_label="B", start=46, end=70),
        SpeakerSpan(candidate_label="C", start=72, end=80),
    ]
    segments = [
        TranscriptSegment(
            session_id=SESSION,
            candidate_id=label,
            start=float(i * 25),
            end=float(i * 25 + 20),
            text=f"{label} makes a substantive point about regulation.",
            confidence=0.92,
        )
        for i, label in enumerate(LABELS)
    ]
    return segments, compute_metrics(SESSION, spans, segments, LABELS)


async def _run(strategy: str = "per_dimension", attempt: int = 1) -> EvaluationState:
    segments, metrics = _fixtures()
    return await run_graph(
        session_id=SESSION,
        topic="Should governments regulate generative AI?",
        gd_type="technical",
        duration_seconds=900,
        candidates=LABELS,
        segments=segments,
        metrics=metrics,
        rolling_state=RollingState.empty(SESSION),
        weights=WEIGHTS,
        strategy=strategy,
        attempt=attempt,
    )


# --------------------------------------------------------------------------
# Graph structure
# --------------------------------------------------------------------------


def test_the_graph_has_the_nodes_the_design_calls_for():
    """Design §20's pipeline, as actual nodes."""
    nodes = set(build_graph().get_graph().nodes)
    assert {"plan", "evaluate", "collect", "aggregate", "synthesise"} <= nodes


def test_the_graph_renders_its_own_topology():
    """A diagram generated from the compiled graph cannot drift from the code."""
    mermaid = render_mermaid()
    assert "plan" in mermaid
    assert "aggregate" in mermaid
    assert "synthesise" in mermaid
    # The fan-out edges are conditional, drawn dotted by mermaid.
    assert "-.->" in mermaid


def test_aggregate_is_a_node_not_an_agent():
    """claude.md invariant 5 survives the framework: the scoring node runs no
    model. Checked structurally — the graph module must not reach the LLM
    layer for aggregation."""
    import inspect

    source = inspect.getsource(graph_module.aggregate_node)
    assert "aggregate_session" in source
    assert "llm" not in source.lower()
    assert "provider" not in source.lower()


# --------------------------------------------------------------------------
# Running the graph
# --------------------------------------------------------------------------


async def test_a_graph_run_scores_every_candidate():
    set_llm_provider(StubLLM())
    final = await _run()

    assert set(final["scores"]) == set(LABELS)
    for label in LABELS:
        assert 0 <= final["scores"][label].overall <= 100
    assert final["failures"] == []


async def test_the_fan_out_makes_one_branch_per_specialist():
    """`Send` fan-out is dynamic — the roster is only known after `plan`."""
    llm = StubLLM()
    set_llm_provider(llm)
    await _run(strategy="per_dimension")

    # Evaluator calls plus one synthesis call per candidate.
    evaluator_calls = [c for c in llm.calls if "tools" not in c]
    assert len(evaluator_calls) == len(select_agents("technical"))


async def test_the_per_candidate_strategy_fans_out_wider():
    llm = StubLLM()
    set_llm_provider(llm)
    await _run(strategy="per_candidate")

    evaluator_calls = [c for c in llm.calls if "tools" not in c]
    assert len(evaluator_calls) == len(select_agents("technical")) * len(LABELS)


async def test_the_score_is_recomputable_from_the_graphs_own_outputs():
    """The graph did not invent a number: the same aggregator on the same
    outputs reproduces it exactly."""
    set_llm_provider(StubLLM())
    final = await _run()

    for label, score in final["scores"].items():
        recomputed = aggregate_candidate(label, final["outputs_by_candidate"][label], WEIGHTS)
        assert recomputed.overall == pytest.approx(score.overall)


async def test_synthesis_runs_once_per_candidate():
    set_llm_provider(StubLLM())
    final = await _run()

    syntheses = dict(final["syntheses"])
    assert set(syntheses) == set(LABELS)
    for result in syntheses.values():
        assert result.error is None
        assert [c["tool"] for c in result.tool_calls] == ["compute_final_score", "score_if"]


async def test_each_attempt_gets_a_clean_run():
    """Regression: `runs` uses an `operator.add` reducer, so re-running on the
    same checkpointer thread appended to the previous run and returned every
    candidate twice. Each attempt must start fresh."""
    set_llm_provider(StubLLM())
    first = await _run(attempt=1)
    second = await _run(attempt=2)

    for label in LABELS:
        assert len(first["outputs_by_candidate"][label]) == len(
            second["outputs_by_candidate"][label]
        )
        # One assessment per dimension, not two.
        dimensions = [o.dimension for o in second["outputs_by_candidate"][label]]
        assert len(dimensions) == len(set(dimensions))


async def test_a_dead_specialist_is_isolated_to_its_branch():
    """A failing node must not abort the sibling branches (NFR-04)."""

    class OneDown:
        name = "one-down"
        model = "test"

        async def complete_json(self, *, system, prompt, schema, max_tokens=None):
            if "YOUR DIMENSION: behavior" in system:
                raise LLMError("this specialist is down")
            return await StubLLM().complete_json(
                system=system, prompt=prompt, schema=schema, max_tokens=max_tokens
            )

        async def complete_with_tools(self, **kwargs):
            return await StubLLM().complete_with_tools(**kwargs)

    set_llm_provider(OneDown())
    final = await _run()

    assert final["failures"], "the dead specialist should be reported"
    assert all("behavior" in f for f in final["failures"])
    # Everyone still gets a score; behaviour's weight is redistributed.
    assert set(final["scores"]) == set(LABELS)
    for score in final["scores"].values():
        assert "behavior" in score.missing_dimensions
        assert score.needs_human_review is True


async def test_a_total_failure_ends_the_graph_without_inventing_a_report():
    """The conditional edge to END is what stops the graph fabricating scores
    when nothing succeeded."""
    set_llm_provider(StubLLM(fail=True))
    final = await _run()

    assert not any(final["outputs_by_candidate"].values())
    assert "scores" not in final or not final["scores"]
