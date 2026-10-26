"""Report assembly (FR-29, FR-30, design §33, §34).

Purpose:     Turns finished results (aggregated scores, agent verdicts, measured
             metrics, the Observer's final state) into the structures the user
             sees: a report per candidate and one for the group.
Why needed:  The report is what a candidate actually reads. Building it from
             stored data with no model call means it can be regenerated at any
             time and come out identical, and every line of feedback traces back
             to timestamped evidence or a measured number (explainability).
Contains:
  - `DOMINANT_SHARE`, `UNDER_PARTICIPATION_SHARE`  thresholds for "held the
        floor" and "barely spoke" (design §34).
  - `DimensionDetail`, `CandidateReport`, `GroupReport`  the report shapes, with
        `to_dict` for storage and the API.
  - `_rank_evidence`, `_clock`, `_quoted`, `_dedupe`  small helpers (order
        evidence, format time, shorten quotes, remove repeats).
  - `entry_moments`, `_moment_lines`  find moments someone turned to the
        candidate by name, so a quiet candidate gets concrete openings.
  - `_metric_observations`  feedback lines produced by arithmetic on metrics.
  - `build_candidate_report`  assemble one candidate's full report.
  - `_discussion_quality`, `_consensus`, `build_group_report`  the group-level
        summary, read from scores and the Observer's state.
Used by:     evaluation/service.py and tests/test_report_quiet.py.
Depends on:  common/contracts.py, evaluation/aggregator.py (`AggregatedScore`),
             metrics/engine.py (`SessionMetrics`).

Pure functions over already-computed results: aggregated scores, agent outputs
and metrics in, report structures out. Nothing here calls a model, which means
the report can be regenerated from stored data at any time and will come out
identical (design §40).

Feedback strings are assembled from the agents' own `strengths` / `weaknesses`
plus the deterministic metrics — the explainability requirement (NFR-08,
claude.md invariant 6) means every line traces back to either a timestamped
piece of evidence or a measured number.
"""

import re  # sentence splitting and text clean-up
from dataclasses import asdict, dataclass, field  # report containers and dict export

from services.common.contracts import CandidateMetrics, EvaluationOutput, RollingState
from services.evaluation.aggregator import AggregatedScore  # computed scores to present
from services.metrics.engine import SessionMetrics  # measured numbers shown in the report

# Design §34: who held the floor, and who barely did.
DOMINANT_SHARE = 0.30
UNDER_PARTICIPATION_SHARE = 0.10


@dataclass(slots=True)
class DimensionDetail:
    """One scored dimension on a candidate report, with its strengths and evidence."""

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
    # M14 EC-14.1: moments someone turned to this candidate by name — openings.
    entry_moments: list[dict] = field(default_factory=list)
    # Change against the candidate's own earlier sessions (coaching only, see progress.py).
    progress: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        """The report as a plain dict for storage and the API."""
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
        """The group report as a plain dict for storage and the API."""
        return asdict(self)


def _rank_evidence(outputs: list[EvaluationOutput]) -> list[dict]:
    """Flatten evidence across dimensions, most confident first."""
    # Gather every evidence item from every dimension, tagged with where it came from.
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
    # Most confident first; ties broken by time so the order is stable.
    return sorted(items, key=lambda i: (-i["confidence"], i["timestamp"]))


def _clock(seconds: float) -> str:
    """Format seconds as m:ss."""
    # Clamp negatives to zero so a bad timestamp can't print "-1:-5".
    whole = int(max(seconds, 0))
    return f"{whole // 60}:{whole % 60:02d}"


def entry_moments(
    segments: list, candidate_label: str, display_name: str, speaker_names: dict[str, str]
) -> list[dict]:
    """Moments someone else turned to this candidate by name (M14 EC-14.1).

    A ready-made opening the candidate could have taken — the moderator's "Asha,
    what's your take?" — found in the transcript, so a quiet candidate's debrief
    can point at real moments instead of telling them to "participate more".
    """
    # Match on the candidate's first name; too-short names would match by accident.
    first = (display_name or "").split()[0] if (display_name or "").split() else ""
    if len(first) < 2:
        return []
    named = re.compile(rf"\b{re.escape(first)}\b", re.IGNORECASE)
    # Every line by someone else that mentions them by name is a potential opening.
    return [
        {
            "timestamp": _clock(segment.start),
            "speaker": speaker_names.get(segment.candidate_id, segment.candidate_id),
            "line": segment.text,
            # The invitation itself: the sentence that names them.
            "quote": next(
                (s for s in re.split(r"(?<=[.!?])\s+", segment.text) if named.search(s)),
                segment.text,
            ),
        }
        for segment in segments
        if segment.candidate_id != candidate_label and named.search(segment.text or "")
    ]


def _quoted(line: str, words: int = 20) -> str:
    """The first `words` words of a line, with an ellipsis when it was cut."""
    parts = line.split()
    return " ".join(parts[:words]) + ("…" if len(parts) > words else "")


def _moment_lines(moments: list[dict], limit: int) -> list[str]:
    """Render up to `limit` entry moments as readable report lines."""
    lines = []
    # Turn each opening into one friendly, specific sentence.
    for m in moments[:limit]:
        quote = _quoted(m.get("quote") or m["line"])
        lines.append(
            f'At {m["timestamp"]}, {m["speaker"]} turned to you: "{quote}" — a ready-made '
            'moment to come in. Even "I\'d add one thing…" would have done it.'
        )
    return lines


def _metric_observations(
    metrics: CandidateMetrics, moments: list[dict] | None = None
) -> list[str]:
    """Feedback lines that come from arithmetic rather than a model."""
    moments = moments or []
    lines: list[str] = []
    share = metrics.participation_ratio * 100
    if metrics.turns == 0:
        # EC-14.1: someone who said nothing gets encouragement and real openings,
        # never disappointment. Nothing else here applies to them.
        return [
            "You didn't come into the discussion this time. That is where nearly everyone "
            "starts, and it is the quickest thing to change: one sentence in the first two "
            "minutes gets you in.",
            *_moment_lines(moments, 3),
        ]
    # Talking far above an even share: suggest leaving room for others.
    if metrics.participation_ratio >= DOMINANT_SHARE:
        lines.append(
            f"You held {share:.0f}% of the speaking time — well above an even share. "
            "Leaving more space for others usually raises a group's overall quality."
        )
    # Talking very little: suggest earlier entry points, using real openings if any.
    elif metrics.participation_ratio <= UNDER_PARTICIPATION_SHARE:
        lines.append(
            f"You spoke for {share:.0f}% of the discussion, less than an even share. Look "
            "for earlier ways in: right after a question, or when someone finishes a point "
            "you have a view on."
        )
        lines.extend(_moment_lines(moments, 2))
    # Repeated interruptions.
    if metrics.interruptions >= 3:
        lines.append(
            f"You interrupted {metrics.interruptions} times. Letting a speaker finish "
            "tends to make a disagreement land harder, not softer."
        )
    # No questions asked despite taking several turns.
    if metrics.questions == 0 and metrics.turns > 2:
        lines.append(
            "You asked no questions. Questions are one of the cheapest ways to shift a "
            "discussion's direction and are read as leadership."
        )
    # Never built on anyone else's point.
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
    moments: list[dict] | None = None,
) -> CandidateReport:
    """FR-29 + FR-30. `moments`: where someone turned to this candidate by name."""
    by_dimension = {output.dimension: output for output in outputs}
    # One detail block per scored dimension: the numbers plus that agent's evidence.
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

    # Merge strengths/weaknesses across agents, dropping duplicates; add measured feedback.
    strengths = _dedupe(s for output in outputs for s in output.strengths)
    weaknesses = _dedupe(w for output in outputs for w in output.weaknesses)
    observations = _metric_observations(metrics, moments)

    if metrics.turns == 0:
        # EC-14.1: the evaluators can only say "did not contribute" five ways; the
        # debrief for someone who said nothing is about getting in, and it is kind.
        return CandidateReport(
            candidate_label=candidate_label,
            display_name=display_name,
            overall_score=score.overall,
            dimensions=details,
            strengths=strengths,
            weaknesses=observations,
            evidence=_rank_evidence(outputs),
            metrics=metrics.model_dump(),
            feedback=" ".join(observations),
            needs_human_review=score.needs_human_review,
            review_reasons=score.review_reasons,
            synthesis=synthesis or {},
            penalty=score.penalty,
            interruption_times=score.interruption_times,
            entry_moments=moments or [],
        )

    # Build the short plain-English summary paragraph, one sentence per fact.
    feedback_parts = [
        f"Overall: {score.overall:.0f}/100 across {len(score.weights)} weighted dimensions."
    ]
    if strengths:
        feedback_parts.append("What worked: " + "; ".join(strengths[:3]) + ".")
    if weaknesses:
        feedback_parts.append("What to work on: " + "; ".join(weaknesses[:3]) + ".")
    # Be explicit about any points lost to interruptions.
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
        entry_moments=moments or [],
    )


def _dedupe(items) -> list[str]:
    """Remove repeats from a list of strings, keeping first-seen order."""
    seen: list[str] = []
    for item in items:
        cleaned = item.strip()
        # Case-insensitive de-duplication; blank strings are dropped.
        if cleaned and cleaned.lower() not in {s.lower() for s in seen}:
            seen.append(cleaned)
    return seen


def _discussion_quality(scores: dict[str, AggregatedScore]) -> str:
    """A one-word rating of the whole discussion, derived from the candidates' scores."""
    if not scores:
        return "unknown"
    # Bucket the average overall score into high / moderate / low.
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
    # No arguments recorded means there is nothing to have agreed on.
    if not state.main_arguments:
        return "not established"
    # Arguments but no open questions: the group got there.
    if not state.unresolved_questions:
        return "reached"
    # More open questions than arguments: effectively no agreement.
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
    # The Observer's most recent interaction note stands in for the turning point.
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
