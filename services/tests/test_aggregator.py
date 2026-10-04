"""Deterministic score aggregation (design §31, §32, claude.md invariant 5).

The final number is arithmetic over agent outputs, so it is testable exactly —
these check the formula, the weighting policy, and the two documented edge
cases (critical thinking has no weight of its own; a missing dimension
renormalises rather than scoring zero).
"""

import pytest

from services.common.contracts import EvaluationOutput
from services.evaluation.aggregator import (
    LOW_CONFIDENCE_THRESHOLD,
    aggregate_candidate,
    aggregate_session,
    normalise_weights,
)
from services.session.models import DEFAULT_WEIGHTS

WEIGHTS = dict(DEFAULT_WEIGHTS)


def output(dimension: str, score: float, confidence: float = 0.9) -> EvaluationOutput:
    return EvaluationOutput(
        candidate_id="A",
        dimension=dimension,
        score=score,
        confidence=confidence,
        strengths=["did a thing"],
        weaknesses=["could do it better"],
        evidence=[{"timestamp": "05:14", "summary": "said something relevant"}],
    )


def test_the_weighted_formula_matches_the_design():
    """0.30·Content + 0.20·Comm + 0.20·Leadership + 0.20·Collab + 0.10·Behavior."""
    outputs = [
        output("content", 8.0),
        output("communication", 7.0),
        output("leadership", 6.0),
        output("collaboration", 9.0),
        output("behavior", 5.0),
    ]
    result = aggregate_candidate("A", outputs, WEIGHTS)

    # 2.4 + 1.4 + 1.2 + 1.8 + 0.5 = 7.3, reported out of 100.
    expected = 0.30 * 8 + 0.20 * 7 + 0.20 * 6 + 0.20 * 9 + 0.10 * 5
    assert result.overall == pytest.approx(expected * 10, abs=0.01)
    assert result.overall == pytest.approx(73.0, abs=0.01)
    assert result.needs_human_review is False


def test_a_perfect_and_a_zero_card_map_to_the_scale_ends():
    top = aggregate_candidate("A", [output(d, 10.0) for d in WEIGHTS], WEIGHTS)
    bottom = aggregate_candidate("A", [output(d, 0.0) for d in WEIGHTS], WEIGHTS)
    assert top.overall == pytest.approx(100.0)
    assert bottom.overall == pytest.approx(0.0)


def test_critical_thinking_is_averaged_into_content():
    """It has no weight of its own; the documented policy folds it into content."""
    outputs = [
        output("content", 8.0),
        output("critical_thinking", 6.0),
        output("communication", 7.0),
        output("leadership", 7.0),
        output("collaboration", 7.0),
        output("behavior", 7.0),
    ]
    result = aggregate_candidate("A", outputs, WEIGHTS)
    assert result.dimensions["content"] == pytest.approx(7.0)  # mean of 8 and 6
    assert "critical_thinking" not in result.dimensions


def test_critical_thinking_stands_in_when_no_content_agent_ran():
    """The HR/social roster (design §22) has no Content agent."""
    outputs = [
        output("critical_thinking", 6.0),
        output("communication", 7.0),
        output("leadership", 7.0),
        output("collaboration", 7.0),
        output("behavior", 7.0),
    ]
    result = aggregate_candidate("A", outputs, WEIGHTS)
    assert result.dimensions["content"] == pytest.approx(6.0)
    assert result.missing_dimensions == []


def test_a_missing_dimension_renormalises_rather_than_scoring_zero():
    """A failed agent is the system's problem, not the candidate's."""
    outputs = [output("content", 8.0), output("communication", 8.0)]
    result = aggregate_candidate("A", outputs, WEIGHTS)

    # 0.30 and 0.20 renormalise to 0.6 / 0.4, so an all-8 card still scores 80.
    assert result.overall == pytest.approx(80.0)
    assert result.weights["content"] == pytest.approx(0.6)
    assert result.weights["communication"] == pytest.approx(0.4)
    assert set(result.missing_dimensions) == {"leadership", "collaboration", "behavior"}
    assert result.needs_human_review is True


def test_applied_weights_always_sum_to_one():
    outputs = [output("content", 5.0), output("behavior", 5.0)]
    result = aggregate_candidate("A", outputs, WEIGHTS)
    assert sum(result.weights.values()) == pytest.approx(1.0)


def test_two_agents_on_one_dimension_are_averaged():
    outputs = [output("content", 9.0), output("content", 5.0)]
    result = aggregate_candidate("A", outputs, WEIGHTS)
    assert result.dimensions["content"] == pytest.approx(7.0)


def test_low_confidence_routes_to_human_review():
    """Design §51, §52."""
    low = LOW_CONFIDENCE_THRESHOLD - 0.2
    outputs = [output(d, 7.0, confidence=low) for d in WEIGHTS]
    result = aggregate_candidate("A", outputs, WEIGHTS)
    assert result.confidence == pytest.approx(low, abs=0.001)
    assert result.needs_human_review is True
    assert "confidence" in result.review_reasons[0]


def test_no_outputs_at_all_is_flagged_not_scored_zero():
    result = aggregate_candidate("A", [], WEIGHTS)
    assert result.overall == 0.0
    assert result.needs_human_review is True
    assert result.review_reasons == ["no dimension produced a score"]


def test_a_custom_rubric_changes_the_answer():
    """Weights are admin-configurable per session (FR-03)."""
    content_heavy = {
        "content": 0.60,
        "communication": 0.10,
        "leadership": 0.10,
        "collaboration": 0.10,
        "behavior": 0.10,
    }
    outputs = [output("content", 10.0)] + [output(d, 0.0) for d in WEIGHTS if d != "content"]
    assert aggregate_candidate("A", outputs, content_heavy).overall == pytest.approx(60.0)
    assert aggregate_candidate("A", outputs, WEIGHTS).overall == pytest.approx(30.0)


def test_aggregation_is_reproducible():
    outputs = [output(d, 7.5) for d in WEIGHTS]
    first = aggregate_candidate("A", outputs, WEIGHTS)
    second = aggregate_candidate("A", outputs, WEIGHTS)
    assert first.overall == second.overall
    assert first.weights == second.weights


def test_session_aggregation_covers_every_candidate():
    per_candidate = {
        "A": [output(d, 8.0) for d in WEIGHTS],
        "B": [output(d, 4.0) for d in WEIGHTS],
    }
    results = aggregate_session(per_candidate, WEIGHTS)
    assert results["A"].overall == pytest.approx(80.0)
    assert results["B"].overall == pytest.approx(40.0)


def test_normalise_weights_scales_to_one():
    assert normalise_weights({"content": 2.0, "behavior": 2.0}) == {
        "content": 0.5,
        "behavior": 0.5,
    }


def test_normalise_weights_rejects_a_degenerate_rubric():
    with pytest.raises(ValueError):
        normalise_weights({"content": 0.0})
