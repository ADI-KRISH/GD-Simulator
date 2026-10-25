"""Deterministic score aggregation (FR-28, design §31, §32).

Purpose:     Turns the per-dimension scores the evaluator agents produced into
             one 0-100 overall score per candidate, using a fixed weighted
             formula (Content 30 / Communication 20 / Leadership 20 /
             Collaboration 20 / Behavior 10 by default) and an interruption penalty.
Why needed:  This is the line that keeps scoring honest: models give evidence and
             dimension scores, but only this plain code produces the final number,
             so a score is reproducible and comparable across sessions
             (claude.md invariant 5). Without it the overall score would be
             whatever a model felt like saying.
Contains:
  - `WEIGHTED_DIMENSIONS`, `DIMENSION_ALIASES`, `LOW_CONFIDENCE_THRESHOLD`  the
        policy constants (which dimensions count, critical thinking -> content,
        when to flag for human review).
  - `AggregatedScore`      one candidate's final result and how it was reached.
  - `_fold_aliases`        merges aliased dimensions before weighting.
  - `interruption_penalty` points deducted for interruptions (linear, capped).
  - `aggregate_candidate`  the core formula for one candidate.
  - `aggregate_session`    applies it to every candidate.
  - `normalise_weights`    makes a rubric sum to 1.0 so weights are never skewed.
Used by:     evaluation/graph.py, group_synthesis.py, report.py, service.py,
             synthesis.py, and tests (test_aggregator.py, test_synthesis.py,
             test_group_synthesis.py, test_langgraph_engine.py, test_report_quiet.py).
Depends on:  common/contracts.py (`EvaluationOutput`); reads penalty settings
             from common/config.py at call time.

No LLM runs in this module and none may. Agents emit per-dimension scores with
evidence; this code applies the weighting policy. That separation is what makes
a score reproducible, debuggable, and comparable across sessions (design §32,
claude.md invariant 5).

Two policy decisions are made explicit here rather than buried:

**Critical thinking has no weight of its own.** The rubric in design §31 has
five weighted dimensions, but design §22 selects a Critical Thinking agent for
technical and HR/social GDs. It is therefore scored under the `content` weight:
when both ran, the two are averaged; when only critical thinking ran (the
HR/social roster has no Content agent), it stands in for content. This is a
choice, not a derivation — it is stated so it can be argued with and changed in
one place.

**Missing dimensions renormalise.** If an agent fails, its weight is redistributed
across the dimensions that did produce a score, rather than scoring the missing
one as zero. A failed agent is an evaluation problem; scoring it zero would
turn it into the candidate's problem.
"""

from dataclasses import dataclass, field  # AggregatedScore container

from services.common.contracts import EvaluationOutput  # validated agent outputs

# Dimensions that carry weight in the rubric.
WEIGHTED_DIMENSIONS = ("content", "communication", "leadership", "collaboration", "behavior")

# Dimensions scored under another dimension's weight. See the module docstring.
DIMENSION_ALIASES = {"critical_thinking": "content"}

# Below this, design §51/§52 route the result to human review rather than
# publishing it as a confident judgement.
LOW_CONFIDENCE_THRESHOLD = 0.60


@dataclass(slots=True)
class AggregatedScore:
    """One candidate's final result. Every number here came from arithmetic."""

    candidate_label: str
    overall: float
    dimensions: dict[str, float]
    weights: dict[str, float]
    confidence: float
    needs_human_review: bool
    missing_dimensions: list[str] = field(default_factory=list)
    review_reasons: list[str] = field(default_factory=list)
    # Interruption penalty (points off the 0-100 overall) and what produced it.
    penalty: float = 0.0
    interruptions: int = 0
    interruption_times: list[float] = field(default_factory=list)


def _fold_aliases(
    scores: dict[str, list[float]], confidences: dict[str, list[float]]
) -> tuple[dict[str, list[float]], dict[str, list[float]]]:
    """Merge dimensions that are aliases of another (critical_thinking -> content, §31)."""
    folded_scores: dict[str, list[float]] = {}
    folded_conf: dict[str, list[float]] = {}
    # Re-key each dimension under the one whose weight it borrows (itself if none),
    # keeping scores and their confidences aligned.
    for dimension, values in scores.items():
        target = DIMENSION_ALIASES.get(dimension, dimension)
        folded_scores.setdefault(target, []).extend(values)
        folded_conf.setdefault(target, []).extend(confidences.get(dimension, []))
    return folded_scores, folded_conf


def interruption_penalty(count: int) -> float:
    """Points deducted for `count` interruptions: linear, capped. Pure arithmetic —
    the evaluators and critic can read the count but never change the deduction."""
    from services.common.config import settings

    # Linear cost per interruption, capped so one bad moment can't zero a candidate.
    return round(
        min(settings.interruption_penalty_cap, count * settings.interruption_penalty_points), 2
    )


def aggregate_candidate(
    candidate_label: str,
    outputs: list[EvaluationOutput],
    weights: dict[str, float],
    interruption_times: list[float] | None = None,
) -> AggregatedScore:
    """Apply the weighting policy to one candidate's agent outputs.

    Returns a 0–100 overall alongside the 0–10 per-dimension scores the agents
    reported, so the report can show both without re-deriving either.
    """
    # Group every agent's score and confidence by the dimension it judged.
    raw_scores: dict[str, list[float]] = {}
    raw_confidence: dict[str, list[float]] = {}
    for output in outputs:
        raw_scores.setdefault(output.dimension, []).append(output.score)
        raw_confidence.setdefault(output.dimension, []).append(output.confidence)

    # critical_thinking is scored under content's weight (see module docstring).
    scores, confidences = _fold_aliases(raw_scores, raw_confidence)

    # Average when two agents landed on the same weighted dimension.
    dimension_scores = {
        dimension: round(sum(values) / len(values), 3) for dimension, values in scores.items()
    }
    dimension_confidence = {
        dimension: sum(values) / len(values) for dimension, values in confidences.items() if values
    }

    # Split the rubric into dimensions that produced a score and ones that didn't.
    present = [d for d in weights if d in dimension_scores]
    missing = [d for d in weights if d not in dimension_scores]

    # Nothing scored at all: return an explicit zero flagged for human review.
    if not present:
        return AggregatedScore(
            candidate_label=candidate_label,
            overall=0.0,
            dimensions=dimension_scores,
            weights={},
            confidence=0.0,
            needs_human_review=True,
            missing_dimensions=missing,
            review_reasons=["no dimension produced a score"],
        )

    # Renormalise over the dimensions that actually have a score.
    total_weight = sum(weights[d] for d in present)
    applied = {d: round(weights[d] / total_weight, 6) for d in present}

    # The fixed formula: weighted sum of 0-10 dimension scores (and of confidences).
    overall_ten = sum(dimension_scores[d] * applied[d] for d in present)
    confidence = sum(dimension_confidence.get(d, 0.0) * applied[d] for d in present)

    # Collect reasons a human should double-check this result.
    reasons: list[str] = []
    if missing:
        reasons.append(f"no score for: {', '.join(sorted(missing))}")
    if confidence < LOW_CONFIDENCE_THRESHOLD:
        reasons.append(f"aggregate confidence {confidence:.2f} below {LOW_CONFIDENCE_THRESHOLD}")

    # Interruptions are measured by code, so their penalty is too.
    times = sorted(interruption_times or [])
    penalty = interruption_penalty(len(times))
    return AggregatedScore(
        candidate_label=candidate_label,
        # Design §33 reports out of 100; interruptions cost points off the top.
        overall=round(max(0.0, overall_ten * 10 - penalty), 2),
        penalty=penalty,
        interruptions=len(times),
        interruption_times=times,
        dimensions=dimension_scores,
        weights=applied,
        confidence=round(confidence, 4),
        needs_human_review=bool(reasons),
        missing_dimensions=sorted(missing),
        review_reasons=reasons,
    )


def aggregate_session(
    outputs_by_candidate: dict[str, list[EvaluationOutput]],
    weights: dict[str, float],
    interruptions: dict[str, list[float]] | None = None,
) -> dict[str, AggregatedScore]:
    """Aggregate every candidate's agent outputs into final scores with the fixed formula."""
    interruptions = interruptions or {}
    # Sorted so the result order (and any report built from it) is stable.
    return {
        label: aggregate_candidate(label, outputs, weights, interruptions.get(label))
        for label, outputs in sorted(outputs_by_candidate.items())
    }


def normalise_weights(weights: dict[str, float]) -> dict[str, float]:
    """Scale a rubric to sum to 1.0.

    Session creation already rejects a rubric that does not, but a job may be
    replayed with a rubric stored before that validation existed, and the
    aggregator must never silently apply weights that do not sum to one.
    """
    total = sum(weights.values())
    # A rubric that sums to zero (or less) can't be scaled; refuse rather than guess.
    if total <= 0:
        raise ValueError("rubric weights must be positive")
    return {dimension: value / total for dimension, value in weights.items()}
