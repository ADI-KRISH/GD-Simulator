"""Candidate evidence packets (FR-27, design §23, §49).

Purpose:     Builds the two pieces of text every evaluator agent reads: the
             shared discussion context (same for everyone) and one evidence
             packet per candidate (their lines, their metrics, who they talked to).
Why needed:  Agents must judge each person in the context of the group, with exact
             quotes and measured numbers, without paying to resend the whole
             transcript six times. This module is that selective packaging.
Contains:
  - `CONTEXT_TURNS`        how much surrounding dialogue to quote around a line.
  - `SharedContext`        topic, type, arguments, group dynamics (+ `render`).
  - `EvidencePacket`       one candidate's metrics, lines, exchanges and
                           interaction edges (+ `render`).
  - `_bullets`, `_pairs`, `_dialogue`  small text formatters for the prompt.
  - `build_shared_context` assembles the shared part from the Observer's state.
  - `_exchanges_for`       quotes windows of dialogue around a candidate's lines.
  - `build_packet`, `build_packets`  assemble per-candidate evidence.
Used by:     evaluation/agents/base.py, evaluation/graph.py,
             evaluation/supervisor.py, tests/test_evaluation_batching.py.
Depends on:  common/contracts.py (data shapes), metrics/engine.py (the measured
             numbers), speech/chunking.py (`format_timestamp`).

Design §23 is the answer to a real tension: evaluate a candidate in isolation
and you lose the group context that makes a group discussion meaningful; give
every agent the whole session and you pay for the same transcript six times over
and bury the candidate in noise.

The split it prescribes, implemented here:

* **Shared context** — topic, main arguments, group dynamics, progression.
  Identical for every candidate and every agent, so it is built once.
* **Candidate evidence** — their contributions, what they responded to, what
  was said back to them, their interaction edges, their metrics.

Both are passed to every agent. Neither replaces the raw transcript excerpts:
design §48 requires the evaluator to see the exact exchange, not just a summary
of it.
"""

from dataclasses import dataclass, field  # SharedContext / EvidencePacket containers

from services.common.contracts import CandidateMetrics, RollingState, TranscriptSegment
from services.metrics.engine import SessionMetrics  # measured numbers to include as facts
from services.speech.chunking import format_timestamp  # mm:ss stamps for quoted lines

# How much surrounding dialogue to include around a candidate's own lines. Two
# turns either side is enough to tell whether they answered the question that
# was actually asked, without pulling in the whole session.
CONTEXT_TURNS = 2


@dataclass(slots=True)
class SharedContext:
    """The part of the picture every candidate shares (design §49)."""

    session_id: str
    topic: str
    gd_type: str
    duration_seconds: int
    participants: list[str]
    main_arguments: list[str] = field(default_factory=list)
    discussion_direction: str = ""
    unresolved_questions: list[str] = field(default_factory=list)
    candidate_positions: dict[str, str] = field(default_factory=dict)
    observations: list[str] = field(default_factory=list)

    def render(self) -> str:
        """The shared context as prompt text."""
        return f"""\
TOPIC: {self.topic}
GD TYPE: {self.gd_type}
PARTICIPANTS: {", ".join(self.participants)}
DURATION: {self.duration_seconds // 60} minutes

MAIN ARGUMENTS RAISED
{_bullets(self.main_arguments)}

CANDIDATE POSITIONS
{_bullets([f"{k}: {v}" for k, v in sorted(self.candidate_positions.items())])}

WHERE THE DISCUSSION WENT
{self.discussion_direction or "(not recorded)"}

UNRESOLVED QUESTIONS
{_bullets(self.unresolved_questions)}

SESSION OBSERVATIONS (non-scoring, from the live Observer)
{_bullets(self.observations)}"""


@dataclass(slots=True)
class EvidencePacket:
    """Everything one agent needs about one candidate (design §23)."""

    candidate_label: str
    metrics: CandidateMetrics
    contributions: list[TranscriptSegment] = field(default_factory=list)
    exchanges: list[str] = field(default_factory=list)
    responded_to: dict[str, int] = field(default_factory=dict)
    responses_received: dict[str, int] = field(default_factory=dict)

    def render(self) -> str:
        """The candidate's metrics and own dialogue as prompt text."""
        m = self.metrics
        return f"""\
CANDIDATE {self.candidate_label}

MEASURED BEHAVIOUR (computed in code from speech events — these are facts, not
estimates; do not contradict them)
  speaking time        {m.speaking_seconds:.0f}s ({m.participation_ratio * 100:.1f}% of the floor)
  turns                {m.turns}
  interruptions made   {m.interruptions}
  direct responses     {m.responses}
  questions asked      {m.questions}
  avg response latency {m.response_latency_seconds:.1f}s

INTERACTION GRAPH
  responded to:       {_pairs(self.responded_to) or "nobody"}
  responded to by:    {_pairs(self.responses_received) or "nobody"}

EVERYTHING THIS CANDIDATE SAID
{_dialogue(self.contributions)}

KEY EXCHANGES INVOLVING THIS CANDIDATE (with surrounding context)
{_bullets(self.exchanges)}"""


def _bullets(items: list[str]) -> str:
    """Render items as an indented bullet list, or "(none)"."""
    # An explicit "(none)" tells the model the section is empty, not missing.
    return "\n".join(f"  - {item}" for item in items) if items else "  (none)"


def _pairs(counts: dict[str, int]) -> str:
    """Render {"B": 3} as "B (3x)" joined by commas."""
    return ", ".join(f"{k} ({v}x)" for k, v in sorted(counts.items())) if counts else ""


def _dialogue(segments: list[TranscriptSegment]) -> str:
    """Render a candidate's own lines as timestamped dialogue."""
    # A silent candidate is stated outright so the model can't invent contributions.
    if not segments:
        return "  (this candidate did not speak)"
    # Chronological lines with mm:ss stamps, which the evidence must cite.
    return "\n".join(
        f"  {format_timestamp(s.start)} {s.candidate_id}: {s.text}"
        for s in sorted(segments, key=lambda s: s.start)
    )


def build_shared_context(
    session_id: str,
    topic: str,
    gd_type: str,
    duration_seconds: int,
    participants: list[str],
    state: RollingState,
) -> SharedContext:
    """Build the context every candidate's evaluation shares (topic, type, Observer state)."""
    # Copy the Observer's rolling-state understanding in alongside the session basics.
    return SharedContext(
        session_id=session_id,
        topic=topic,
        gd_type=gd_type,
        duration_seconds=duration_seconds,
        participants=participants,
        main_arguments=state.main_arguments,
        discussion_direction=state.discussion_direction,
        unresolved_questions=state.unresolved_questions,
        candidate_positions=state.candidate_positions,
        observations=state.observations,
    )


def _exchanges_for(
    label: str, segments: list[TranscriptSegment], context_turns: int = CONTEXT_TURNS
) -> list[str]:
    """Windows of dialogue around each of the candidate's contributions.

    This is the design §48 requirement in practice: the evaluator gets the
    actual exchange, so it can tell "challenged respectfully" from
    "misunderstood the point" rather than trusting a summary that says only
    "A challenged B".
    """
    # Work on the transcript in time order; `used` tracks lines already quoted.
    ordered = sorted(segments, key=lambda s: s.start)
    windows: list[str] = []
    used: set[int] = set()

    for index, segment in enumerate(ordered):
        # Only build a window around this candidate's own lines.
        if segment.candidate_id != label:
            continue
        # Take a few turns either side (clamped to the transcript's edges).
        low = max(0, index - context_turns)
        high = min(len(ordered), index + context_turns + 1)
        # Skip a window already fully covered by a previous one — consecutive
        # turns by the same candidate would otherwise repeat the same text.
        span = set(range(low, high))
        if span <= used:
            continue
        used |= span
        # One window = the surrounding lines joined into a single quoted exchange.
        windows.append(
            " | ".join(
                f"{format_timestamp(s.start)} {s.candidate_id}: {s.text}" for s in ordered[low:high]
            )
        )
    return windows


def build_packet(
    label: str, segments: list[TranscriptSegment], metrics: SessionMetrics
) -> EvidencePacket:
    """Assemble one candidate's evidence (design §23)."""
    # Metrics and interaction edges come from the deterministic engine, never an LLM.
    return EvidencePacket(
        candidate_label=label,
        metrics=metrics.per_candidate[label],
        contributions=[s for s in segments if s.candidate_id == label],
        exchanges=_exchanges_for(label, segments),
        responded_to={e.target: e.count for e in metrics.edges_from(label)},
        responses_received={e.source: e.count for e in metrics.edges_to(label)},
    )


def build_packets(
    labels: list[str], segments: list[TranscriptSegment], metrics: SessionMetrics
) -> dict[str, EvidencePacket]:
    """Build one evidence packet per candidate label."""
    # Skip labels with no measured metrics (e.g. someone who never joined).
    return {
        label: build_packet(label, segments, metrics)
        for label in labels
        if label in metrics.per_candidate
    }
