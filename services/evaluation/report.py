"""Report assembly (FR-29, FR-30, design §33, §34).

Pure functions over already-computed results: aggregated scores, agent outputs
and metrics in, report structures out. Nothing here calls a model, which means
the report can be regenerated from stored data at any time and will come out
identical (design §40).

Feedback strings are assembled from the agents' own `strengths` / `weaknesses`
plus the deterministic metrics — the explainability requirement (NFR-08,
claude.md invariant 6) means every line traces back to either a timestamped
piece of evidence or a measured number.
"""

from dataclasses import asdict, dataclass, field

from services.common.contracts import CandidateMetrics, EvaluationOutput, RollingState
from services.evaluation.aggregator import AggregatedScore
from services.metrics.engine import SessionMetrics

# Design §34: who held the floor, and who barely did.
DOMINANT_SHARE = 0.30
UNDER_PARTICIPATION_SHARE = 0.10


@dataclass(slots=True)
class DimensionDetail:
    dimension: str
    score_10: float
    score_100: float
    confidence: float
    weight: float
    strengths: list[str] = field(default_factory=list)
    weaknesses: list[str] = field(default_factory=list)
    evidence: list[dict] = field(default_factory=list)


@dataclass(slots=True)
class CandidateReport:
    """Design §33."""

    candidate_label: str
    display_name: str
    overall_score: float
    dimensions: list[DimensionDetail]
    strengths: list[str]
    weaknesses: list[str]
    evidence: list[dict]
    metrics: dict
    feedback: str
    needs_human_review: bool
    review_reasons: list[str]
    # Design §20 "Final Evaluator": the holistic narrative, written by the
    # synthesis agent from the dimension verdicts. Empty when it was
    # unavailable — the rest of the report stands on its own.
    synthesis: dict = field(default_factory=dict)
    # Points deducted for interrupting, and when each interruption happened.
    penalty: float = 0.0
    interruption_times: list[float] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass(slots=True)
class GroupReport:
    """Design §34."""

    session_id: str
    topic: str
    discussion_quality: str
    main_themes: list[str]
    consensus: str
    dominant_participants: list[str]
    under_participating: list[str]
    turning_point: str
    unresolved_questions: list[str]
    participation: dict[str, float]

    def to_dict(self) -> dict:
        return asdict(self)


def _rank_evidence(outputs: list[EvaluationOutput]) -> list[dict]:
    """Flatten evidence across dimensions, most confident first."""
    items = [
        {
            "dimension": output.dimension,
            "timestamp": item.timestamp,
            "summary": item.summary,
            "confidence": output.confidence,
        }
        for output in outputs
        for item in output.evidence
    ]
    return sorted(items, key=lambda i: (-i["confidence"], i["timestamp"]))


def _metric_observations(metrics: CandidateMetrics) -> list[str]:
    """Feedback lines that come from arithmetic rather than a model."""
    lines: list[str] = []
    share = metrics.participation_ratio * 100
    if metrics.participation_ratio >= DOMINANT_SHARE:
        lines.append(
            f"You held {share:.0f}% of the speaking time — well above an even share. "
            "Leaving more space for others usually raises a group's overall quality."
        )
    elif metrics.participation_ratio <= UNDER_PARTICIPATION_SHARE:
        lines.append(
            f"You spoke for {share:.0f}% of the discussion. Your points landed, but there "
            "were fewer of them than the group average — look for earlier entry points."
        )
    if metrics.interruptions >= 3:
        lines.append(
            f"You interrupted {metrics.interruptions} times. Letting a speaker finish "
            "tends to make a disagreement land harder, not softer."
        )
    if metrics.questions == 0 and metrics.turns > 2:
        lines.append(
            "You asked no questions. Questions are one of the cheapest ways to shift a "
            "discussion's direction and are read as leadership."
        )
    if metrics.responses == 0 and metrics.turns > 1:
        lines.append(
            "None of your contributions directly followed another candidate's point. "
            "Explicitly building on someone reads as collaboration."
        )
    return lines


def build_candidate_report(
    candidate_label: str,
    display_name: str,
    score: AggregatedScore,
    outputs: list[EvaluationOutput],
    metrics: CandidateMetrics,
    synthesis: dict | None = None,
) -> CandidateReport:
    """FR-29 + FR-30."""
    by_dimension = {output.dimension: output for output in outputs}
    details = [
        DimensionDetail(
            dimension=dimension,
            score_10=value,
            score_100=round(value * 10, 1),
            confidence=round(by_dimension[dimension].confidence, 3)
            if dimension in by_dimension
            else 0.0,
            weight=score.weights.get(dimension, 0.0),
            strengths=by_dimension[dimension].strengths if dimension in by_dimension else [],
            weaknesses=by_dimension[dimension].weaknesses if dimension in by_dimension else [],
            evidence=[
                {"timestamp": e.timestamp, "summary": e.summary}
                for e in (by_dimension[dimension].evidence if dimension in by_dimension else [])
            ],
        )
        for dimension, value in sorted(score.dimensions.items())
    ]

    strengths = _dedupe(s for output in outputs for s in output.strengths)
    weaknesses = _dedupe(w for output in outputs for w in output.weaknesses)
    observations = _metric_observations(metrics)

    feedback_parts = [
        f"Overall: {score.overall:.0f}/100 across {len(score.weights)} weighted dimensions."
    ]
    if strengths:
        feedback_parts.append("What worked: " + "; ".join(strengths[:3]) + ".")
    if weaknesses:
        feedback_parts.append("What to work on: " + "; ".join(weaknesses[:3]) + ".")
    if score.penalty:
        feedback_parts.append(
            f"Interrupting cost you {score.penalty:g} points "
            f"({score.interruptions} interruption{'s' if score.interruptions != 1 else ''})."
        )
    feedback_parts.extend(observations)

    return CandidateReport(
        candidate_label=candidate_label,
        display_name=display_name,
        overall_score=score.overall,
        dimensions=details,
        strengths=strengths,
        weaknesses=weaknesses + observations,
        evidence=_rank_evidence(outputs),
        metrics=metrics.model_dump(),
        feedback=" ".join(feedback_parts),
        needs_human_review=score.needs_human_review,
        review_reasons=score.review_reasons,
        synthesis=synthesis or {},
        penalty=score.penalty,
        interruption_times=score.interruption_times,
    )


def _dedupe(items) -> list[str]:
    seen: list[str] = []
    for item in items:
        cleaned = item.strip()
        if cleaned and cleaned.lower() not in {s.lower() for s in seen}:
            seen.append(cleaned)
    return seen


def _discussion_quality(scores: dict[str, AggregatedScore]) -> str:
    if not scores:
        return "unknown"
    average = sum(s.overall for s in scores.values()) / len(scores)
    if average >= 75:
        return "high"
    if average >= 55:
        return "moderate"
    return "low"


def _consensus(state: RollingState) -> str:
    """Read off the rolling state rather than asking a model again.

    Unresolved questions are the observable proxy the Observer already tracked;
    inventing a second judgement here would be a second, unexplainable opinion.
    """
    if not state.main_arguments:
        return "not established"
    if not state.unresolved_questions:
        return "reached"
    if len(state.unresolved_questions) >= len(state.main_arguments):
        return "none"
    return "partial"


def build_group_report(
    session_id: str,
    topic: str,
    scores: dict[str, AggregatedScore],
    metrics: SessionMetrics,
    state: RollingState,
) -> GroupReport:
    """Design §34."""
    turning_point = state.recent_interactions[-1] if state.recent_interactions else "not identified"
    return GroupReport(
        session_id=session_id,
        topic=topic,
        discussion_quality=_discussion_quality(scores),
        main_themes=state.main_arguments[:5],
        consensus=_consensus(state),
        dominant_participants=metrics.dominant(DOMINANT_SHARE),
        under_participating=metrics.under_participating(UNDER_PARTICIPATION_SHARE),
        turning_point=turning_point,
        unresolved_questions=state.unresolved_questions,
        participation={
            label: round(m.participation_ratio, 4) for label, m in metrics.per_candidate.items()
        },
    )
