"""The Final Synthesis agent and its scoring tools (design §20).

Purpose:     Tests `evaluation/synthesis.py`: the two scoring tools
             (`compute_final_score`, `score_if`), the prompt it builds, and what
             happens when the model misbehaves or fails.
Why needed:  The synthesis agent writes narrative but must never influence the
             score. These tests defend that boundary: tools compute from stored
             data, the prompt withholds the overall number, the schema has no
             score field, and a model that tries to smuggle a number out can't.
Contains:    test builders (`output`, `outputs`, `metrics`, `_Seg`) and tests for
             tool arithmetic, no-argument tool, what-if correctness without
             mutation, bad arguments as values, tool crashes, prompt contents,
             schema shape, recorded tool calls, graceful failure, the score-
             smuggling attempt, the candidate's own lines and the keep-good /
             drop-bad item filtering.
Used by:     pytest (run with `pytest services/tests/test_synthesis.py`).
Depends on:  evaluation/synthesis.py and aggregator.py (under test),
             llm/ (stub provider, base types), common/contracts.py,
             session/models.py (`DEFAULT_WEIGHTS`).

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
    own_lines_for,
    synthesise,
)
from services.llm import set_llm_provider
from services.llm.base import LLMResult, ToolInvocation
from services.llm.stub_provider import StubLLM
from services.session.models import DEFAULT_WEIGHTS

WEIGHTS = dict(DEFAULT_WEIGHTS)
LABEL = "A"


def output(dimension: str, score: float, confidence: float = 0.85) -> EvaluationOutput:
    """Build a valid EvaluationOutput for one dimension."""
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
    """A full set of dimension outputs for the test candidate."""
    return [
        output("content", 8.0),
        output("communication", 7.0),
        output("leadership", 4.0),
        output("collaboration", 5.0),
        output("behavior", 6.0),
    ]


def metrics() -> CandidateMetrics:
    """Sample measured metrics for the test candidate."""
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
    # Index the tools by name, call the real one, and compare with a direct aggregator run.
    tools = {t.name: t for t in build_tools(LABEL, outputs(), WEIGHTS)}
    result = await tools["compute_final_score"].invoke({})
    expected = aggregate_candidate(LABEL, outputs(), WEIGHTS)

    assert result["overall_out_of_100"] == expected.overall
    assert result["dimension_scores_out_of_10"] == expected.dimensions
    assert result["weights_applied"] == expected.weights


async def test_compute_final_score_takes_no_arguments():
    """The model names a candidate at most — it can never supply a number."""
    # The input schema is empty and closed, so the model has nowhere to put a number.
    tools = {t.name: t for t in build_tools(LABEL, outputs(), WEIGHTS)}
    schema = tools["compute_final_score"].input_schema
    assert schema["properties"] == {}
    assert schema["additionalProperties"] is False


async def test_score_if_is_hypothetical_and_arithmetically_true():
    """The feature that makes the tool worth having over a static number."""
    tools = {t.name: t for t in build_tools(LABEL, outputs(), WEIGHTS)}
    # Ask: "what if collaboration had been 9?" and compare with the real score.
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
    # Record the real score first, run a what-if, then confirm nothing changed.
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
    # Unknown dimension, score too high, score too low: each must come back as an error dict.
    tools = {t.name: t for t in build_tools(LABEL, outputs(), WEIGHTS)}
    result = await tools["score_if"].invoke(arguments)
    assert "error" in result


async def test_a_raising_handler_becomes_an_error_result():
    """A tool crash must not kill the turn."""
    from services.llm.base import ToolDefinition

    def explode() -> dict:
        """Tool handler that always fails."""
        raise RuntimeError("boom")

    # Wrap the failing handler as a tool; invoking it must return the error, not raise.
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
    # Build the prompt and the true score, then check the score never appears in the prompt.
    prompt = build_prompt(LABEL, outputs(), metrics(), "Should AI be regulated?")
    expected = aggregate_candidate(LABEL, outputs(), WEIGHTS)

    assert str(round(expected.overall)) not in prompt
    # The dimension verdicts and measured behaviour *are* there.
    assert "CONTENT — 8.0/10" in prompt
    assert "28.0% of the floor" in prompt
    assert "compute_final_score" in prompt


def test_the_system_prompt_forbids_inventing_numbers():
    """The synthesis prompt tells the model to use tool numbers only."""
    # Case-insensitive check that the two key rules are in the instructions.
    lowered = SYSTEM_PROMPT.lower()
    assert "you do not decide the score" in lowered
    assert "never state a number you were not given by a tool" in lowered


def test_the_response_schema_has_no_score_field():
    """Structural: there is nowhere for a score to go (claude.md invariant 5)."""
    # The exact set of allowed output fields; none may mention a score.
    properties = set(RESPONSE_SCHEMA["properties"])
    assert properties == {
        "summary",
        "pattern",
        "priority_improvement",
        "caveat",
        "strengths",
        "focus_areas",
        "dimension_notes",
    }
    assert not any("score" in key for key in properties)
    assert RESPONSE_SCHEMA["additionalProperties"] is False


async def test_synthesis_records_the_tool_calls_it_made():
    """Provenance: you can show what the model asked and what it was told."""
    # The stub always calls every tool once, so both calls must be recorded in order.
    set_llm_provider(StubLLM())
    result = await synthesise(LABEL, outputs(), WEIGHTS, metrics(), "topic")

    assert result.error is None
    assert [call["tool"] for call in result.tool_calls] == ["compute_final_score", "score_if"]
    assert "overall_out_of_100" in result.tool_calls[0]["result"]


async def test_a_synthesis_failure_degrades_rather_than_raises():
    """The score, dimensions and evidence are already stored — losing the
    narrative must not lose the report."""
    # With the model down, synthesis returns an "unavailable" result instead of raising.
    set_llm_provider(StubLLM(fail=True))
    result = await synthesise(LABEL, outputs(), WEIGHTS, metrics(), "topic")

    assert isinstance(result, Synthesis)
    assert result.error is not None
    assert result.summary == ""


async def test_no_dimension_scores_means_nothing_to_synthesise():
    """With no dimension scores synthesis reports an error instead of a narrative."""
    set_llm_provider(StubLLM())
    result = await synthesise(LABEL, [], WEIGHTS, metrics(), "topic")
    assert result.error == "no dimension scores to synthesise"


async def test_a_model_inventing_a_score_field_cannot_reach_the_report():
    """Even a model that tries to smuggle a number out has nowhere to put it."""

    class Smuggler:
        """Fake model that smuggles an extra overall_score into its reply."""
        name = "smuggler"
        model = "test"

        async def complete_with_tools(self, *, system, prompt, tools, schema, **kwargs):
            """Return a reply that includes a made-up overall_score."""
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

    # Run synthesis against the cheating model and check the extra fields were discarded.
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
        "strengths",
        "focus_areas",
        "dimension_notes",
        "tool_calls",
        "model",
        "error",
    }


class _Seg:
    """Minimal transcript segment stand-in."""
    def __init__(self, who: str, start: float, text: str) -> None:
        """Store speaker, start time and text."""
        self.candidate_id, self.start, self.text = who, start, text


def test_the_prompt_carries_only_this_candidates_own_lines():
    """Only the candidate's own lines are put in their prompt."""
    # A mixed transcript: only A's two lines (with mm:ss stamps) should be extracted.
    segments = [
        _Seg("A", 5, "Remote work widens the talent pool."),
        _Seg("B", 20, "Hallway chats matter."),
        _Seg("A", 65, "I would add that async forces clearer writing."),
    ]
    lines = own_lines_for("A", segments)
    assert lines == [
        '  00:05 "Remote work widens the talent pool."',
        '  01:05 "I would add that async forces clearer writing."',
    ]
    prompt = build_prompt(LABEL, outputs(), metrics(), "topic", lines)
    assert "YOUR OWN LINES" in prompt
    assert "Hallway chats matter" not in prompt


def test_long_transcripts_keep_the_start_and_the_end():
    """Long transcripts are trimmed but keep the first and last lines."""
    # 30 lines exceed the cap, so the middle is dropped but first and last survive.
    segments = [_Seg("A", i * 10, f"point {i}") for i in range(30)]
    lines = own_lines_for("A", segments)
    assert len(lines) == 12
    assert "point 0" in lines[0] and "point 29" in lines[-1]


def test_the_system_prompt_demands_descriptive_plain_language():
    """The prompt asks for plain, descriptive wording."""
    assert "has not seen the scoring rubric" in SYSTEM_PROMPT
    assert "dimension_notes" in SYSTEM_PROMPT


def test_the_system_prompt_demands_personal_advice():
    """The prompt asks for advice specific to this candidate."""
    assert "pasted into anyone else's report" in SYSTEM_PROMPT


async def test_strengths_and_focus_areas_reach_the_report_and_bad_items_are_dropped():
    """Valid strengths/focus areas reach the report and malformed items are dropped."""
    class Writer:
        """Fake model returning strengths and focus areas, some malformed."""
        name = "writer"
        model = "test"

        async def complete_with_tools(self, *, system, prompt, tools, schema, **kwargs):
            """Return a reply with good and bad strengths/focus areas."""
            return LLMResult(
                data={
                    "summary": "s",
                    "pattern": "p",
                    "priority_improvement": "i",
                    "caveat": "",
                    "strengths": [
                        {
                            "point": "Cited data",
                            "evidence": "00:05 'four percent'",
                            "why_it_helped": "grounded it",
                        },
                        "not an object",
                    ],
                    "focus_areas": [
                        {
                            "area": "Invite quieter voices",
                            "what_happened": "You held 46% of the floor.",
                            "why_it_matters": "Panels read it as not making room.",
                            "what_to_do": "Ask one person a direct question per round.",
                            "practice": "Count your turns in a mock GD; ask on every third.",
                            "try_saying": "Esha, where do you land on this?",
                        },
                        {"what_to_do": "missing the area key"},
                    ],
                },
                model="test",
                tool_calls=[],
            )

    # Only the well-formed strength and focus area should survive the filtering.
    set_llm_provider(Writer())
    result = await synthesise(LABEL, outputs(), WEIGHTS, metrics(), "topic", [])
    assert [s["point"] for s in result.strengths] == ["Cited data"]
    assert [f["area"] for f in result.focus_areas] == ["Invite quieter voices"]
    assert result.focus_areas[0]["try_saying"].startswith("Esha")
    assert result.focus_areas[0]["practice"].startswith("Count")
