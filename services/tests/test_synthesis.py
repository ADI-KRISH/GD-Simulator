"""The Final Synthesis agent and its scoring tools (design §20).

The agent is allowed a holistic view and a narrative voice. It is not allowed
the number. These tests defend that boundary — the tools compute from stored
data, the model supplies only the question, and no path exists by which a
model's opinion reaches `overall`.
"""

import pytest

from services.common.contracts import CandidateMetrics, EvaluationOutput
from services.evaluation.aggregator import aggregate_candidate
from services.evaluation.synthesis import (
    RESPONSE_SCHEMA,
    SYSTEM_PROMPT,
    Synthesis,
    build_prompt,
    build_tools,
    synthesise,
)
from services.llm import set_llm_provider
from services.llm.base import LLMResult, ToolInvocation
from services.llm.stub_provider import StubLLM
from services.session.models import DEFAULT_WEIGHTS

WEIGHTS = dict(DEFAULT_WEIGHTS)
LABEL = "A"


def output(dimension: str, score: float, confidence: float = 0.85) -> EvaluationOutput:
    return EvaluationOutput(
        candidate_id=LABEL,
        dimension=dimension,
        score=score,
        confidence=confidence,
        strengths=[f"did well on {dimension}"],
        weaknesses=[f"could improve {dimension}"],
        evidence=[{"timestamp": "04:20", "summary": f"a moment relevant to {dimension}"}],
    )


def outputs() -> list[EvaluationOutput]:
    return [
        output("content", 8.0),
        output("communication", 7.0),
        output("leadership", 4.0),
        output("collaboration", 5.0),
        output("behavior", 6.0),
    ]


def metrics() -> CandidateMetrics:
    return CandidateMetrics(
        session_id="GD1",
        candidate_id=LABEL,
        speaking_seconds=180,
        participation_ratio=0.28,
        turns=9,
        interruptions=1,
        responses=4,
        questions=2,
    )


# --------------------------------------------------------------------------
# The tools
# --------------------------------------------------------------------------


async def test_compute_final_score_matches_the_deterministic_aggregator():
    """The tool must be the same arithmetic, not a second implementation."""
    tools = {t.name: t for t in build_tools(LABEL, outputs(), WEIGHTS)}
    result = await tools["compute_final_score"].invoke({})
    expected = aggregate_candidate(LABEL, outputs(), WEIGHTS)

    assert result["overall_out_of_100"] == expected.overall
    assert result["dimension_scores_out_of_10"] == expected.dimensions
    assert result["weights_applied"] == expected.weights


async def test_compute_final_score_takes_no_arguments():
    """The model names a candidate at most — it can never supply a number."""
    tools = {t.name: t for t in build_tools(LABEL, outputs(), WEIGHTS)}
    schema = tools["compute_final_score"].input_schema
    assert schema["properties"] == {}
    assert schema["additionalProperties"] is False


async def test_score_if_is_hypothetical_and_arithmetically_true():
    """The feature that makes the tool worth having over a static number."""
    tools = {t.name: t for t in build_tools(LABEL, outputs(), WEIGHTS)}
    result = await tools["score_if"].invoke(
        {"dimension": "collaboration", "hypothetical_score": 9.0}
    )

    actual = aggregate_candidate(LABEL, outputs(), WEIGHTS)
    assert result["hypothetical"] is True
    assert result["actual_overall"] == actual.overall
    # collaboration is weighted 0.20, so +4.0 on a 0-10 scale is +8 out of 100.
    assert result["difference"] == pytest.approx(8.0, abs=0.01)
    assert result["hypothetical_overall"] == pytest.approx(actual.overall + 8.0, abs=0.01)


async def test_score_if_does_not_mutate_the_real_result():
    """A what-if must leave the stored evaluation untouched."""
    stored = outputs()
    tools = {t.name: t for t in build_tools(LABEL, stored, WEIGHTS)}
    before = aggregate_candidate(LABEL, stored, WEIGHTS).overall

    await tools["score_if"].invoke({"dimension": "content", "hypothetical_score": 10.0})

    assert aggregate_candidate(LABEL, stored, WEIGHTS).overall == before
    assert [o.score for o in stored] == [8.0, 7.0, 4.0, 5.0, 6.0]


@pytest.mark.parametrize(
    "arguments",
    [
        {"dimension": "vibes", "hypothetical_score": 9.0},
        {"dimension": "content", "hypothetical_score": 42.0},
        {"dimension": "content", "hypothetical_score": -1.0},
    ],
)
async def test_score_if_rejects_bad_arguments_as_a_value(arguments):
    """A bad argument is returned to the model to correct, not raised."""
    tools = {t.name: t for t in build_tools(LABEL, outputs(), WEIGHTS)}
    result = await tools["score_if"].invoke(arguments)
    assert "error" in result


async def test_a_raising_handler_becomes_an_error_result():
    """A tool crash must not kill the turn."""
    from services.llm.base import ToolDefinition

    def explode() -> dict:
        raise RuntimeError("boom")

    tool = ToolDefinition(
        name="explode", description="", input_schema={"type": "object"}, handler=explode
    )
    assert "RuntimeError: boom" in (await tool.invoke({}))["error"]


# --------------------------------------------------------------------------
# The agent
# --------------------------------------------------------------------------


def test_the_prompt_withholds_the_overall_score():
    """If the number were in the prompt the model could quote it without the
    tool, and the provenance trail would be a fiction."""
    prompt = build_prompt(LABEL, outputs(), metrics(), "Should AI be regulated?")
    expected = aggregate_candidate(LABEL, outputs(), WEIGHTS)

    assert str(round(expected.overall)) not in prompt
    # The dimension verdicts and measured behaviour *are* there.
    assert "CONTENT — 8.0/10" in prompt
    assert "28.0% of the floor" in prompt
    assert "compute_final_score" in prompt


def test_the_system_prompt_forbids_inventing_numbers():
    lowered = SYSTEM_PROMPT.lower()
    assert "you do not decide the score" in lowered
    assert "never state a number you were not given by a tool" in lowered


def test_the_response_schema_has_no_score_field():
    """Structural: there is nowhere for a score to go (claude.md invariant 5)."""
    properties = set(RESPONSE_SCHEMA["properties"])
    assert properties == {"summary", "pattern", "priority_improvement", "caveat"}
    assert not any("score" in key for key in properties)
    assert RESPONSE_SCHEMA["additionalProperties"] is False


async def test_synthesis_records_the_tool_calls_it_made():
    """Provenance: you can show what the model asked and what it was told."""
    set_llm_provider(StubLLM())
    result = await synthesise(LABEL, outputs(), WEIGHTS, metrics(), "topic")

    assert result.error is None
    assert [call["tool"] for call in result.tool_calls] == ["compute_final_score", "score_if"]
    assert "overall_out_of_100" in result.tool_calls[0]["result"]


async def test_a_synthesis_failure_degrades_rather_than_raises():
    """The score, dimensions and evidence are already stored — losing the
    narrative must not lose the report."""
    set_llm_provider(StubLLM(fail=True))
    result = await synthesise(LABEL, outputs(), WEIGHTS, metrics(), "topic")

    assert isinstance(result, Synthesis)
    assert result.error is not None
    assert result.summary == ""


async def test_no_dimension_scores_means_nothing_to_synthesise():
    set_llm_provider(StubLLM())
    result = await synthesise(LABEL, [], WEIGHTS, metrics(), "topic")
    assert result.error == "no dimension scores to synthesise"


async def test_a_model_inventing_a_score_field_cannot_reach_the_report():
    """Even a model that tries to smuggle a number out has nowhere to put it."""

    class Smuggler:
        name = "smuggler"
        model = "test"

        async def complete_with_tools(self, *, system, prompt, tools, schema, **kwargs):
            return LLMResult(
                data={
                    "summary": "s",
                    "pattern": "p",
                    "priority_improvement": "i",
                    "caveat": "c",
                    "overall_score": 99.0,
                    "final": 99.0,
                },
                model="test",
                tool_calls=[ToolInvocation(name="compute_final_score", arguments={}, result={})],
            )

    set_llm_provider(Smuggler())
    result = await synthesise(LABEL, outputs(), WEIGHTS, metrics(), "topic")

    body = result.to_dict()
    assert "overall_score" not in body
    assert "final" not in body
    assert set(body) == {
        "summary",
        "pattern",
        "priority_improvement",
        "caveat",
        "tool_calls",
        "model",
        "error",
    }
