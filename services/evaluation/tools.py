"""Evidence tools for the agentic evaluators, and the critic's audit.

Purpose:     Gives the evaluator agents read-only "lookup" tools (read the
             transcript, check a candidate's metrics, find quotes, see who
             interrupted whom) and provides the critic's deterministic check that
             each piece of cited evidence is real.
Why needed:  A model scoring from a prompt alone can misquote or invent evidence.
             Tools let it verify claims against the actual transcript, and the
             audit rejects evidence pointing at a moment the candidate never
             spoke. Because every tool only returns code-computed data, a tool can
             never hand the model a number the metrics engine didn't produce
             (claude.md invariant 4).
Contains:
  - `TIMESTAMP_TOLERANCE_SECONDS`  slack for "mm:ss" rounding when checking evidence.
  - `TurnRecord`           plain-value view of a tracked turn for the tools.
  - `parse_timestamp`      "mm:ss" / "h:mm:ss" -> seconds (or None).
  - `_clock`               seconds -> "mm:ss".
  - `build_evidence_tools` builds the five tools (`get_transcript_range`,
                           `get_candidate_metrics`, `get_turns`,
                           `get_interactions`, `find_quotes`) for one session.
  - `audit_evidence`       the critic check: do cited moments match real speech?
Used by:     evaluation/graph.py (builds tools, runs the critic) and
             evaluation/service.py.
Depends on:  common/contracts.py, llm/base.py (`ToolDefinition`),
             metrics/engine.py (`SessionMetrics`).

The specialists no longer score from a prompt alone: they can look things up.
Every handler here is read-only and answers from data already persisted in code
(transcript segments, deterministic metrics, tracked turns) — so a tool can never
hand the model a number the metrics engine did not compute (claude.md invariant 4).

`audit_evidence` is the critic's deterministic half: it checks that each piece of
cited evidence points at a moment the candidate actually spoke.
"""

import re  # keyword/quote matching
from dataclasses import dataclass  # TurnRecord container
from typing import Any  # JSON-like tool results

from services.common.contracts import EvaluationOutput, TranscriptSegment
from services.llm.base import ToolDefinition  # how a tool is described to the model
from services.metrics.engine import SessionMetrics  # measured numbers the tools expose

# Cited timestamps may be a little off the segment boundaries (mm:ss rounding).
TIMESTAMP_TOLERANCE_SECONDS = 15.0

# Critic: high confidence needs more than one cited moment; quotes shorter than
# this are too generic to check fairly.
OVERCONFIDENT_AT = 0.85
MIN_EVIDENCE_FOR_CONFIDENCE = 2
MIN_QUOTE_WORDS = 4


def _norm(text: str) -> str:
    """Lower-case words only, so punctuation and spacing can't break a quote match."""
    return " ".join(re.findall(r"\w+", text.lower()))


_LABEL = {"type": "string", "description": "Candidate label such as A, B, C."}


@dataclass(frozen=True, slots=True)
class TurnRecord:
    """The slice of a tracked turn the tools expose (a plain value, not an ORM row)."""

    candidate_label: str
    start: float
    end: float
    interrupted_label: str | None = None
    reply_to_label: str | None = None


def parse_timestamp(value: str) -> float | None:
    """'mm:ss' or 'h:mm:ss' -> seconds, or None if it is not a timestamp."""
    match = re.fullmatch(r"\s*(?:(\d+):)?(\d{1,2}):(\d{2})(?:\.\d+)?\s*", value or "")
    if not match:
        return None
    # Optional hours group defaults to 0 so "mm:ss" and "h:mm:ss" share one path.
    hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _clock(seconds: float) -> str:
    """Format seconds as mm:ss."""
    return f"{int(seconds) // 60:02d}:{int(seconds) % 60:02d}"


def build_evidence_tools(
    segments: list[TranscriptSegment],
    metrics: SessionMetrics,
    turns: list[TurnRecord],
) -> list[ToolDefinition]:
    """Lookup tools bound to one finished session."""

    def get_transcript_range(
        start_seconds: float, end_seconds: float, candidate_label: str | None = None
    ) -> list[dict[str, Any]]:
        """Tool: transcript lines in a time window, optionally for one candidate."""
        # Lines overlapping the window, in time order, capped at 40 to keep replies small.
        return [
            {"speaker": s.candidate_id, "at": _clock(s.start), "text": s.text}
            for s in sorted(segments, key=lambda s: s.start)
            if s.end >= start_seconds
            and s.start <= end_seconds
            and (candidate_label is None or s.candidate_id == candidate_label)
        ][:40]

    def get_candidate_metrics(candidate_label: str) -> dict[str, Any]:
        """Tool: a candidate's measured metrics (speaking time, turns, interruptions, ...)."""
        found = metrics.per_candidate.get(candidate_label)
        # An unknown label returns an error value the model can react to.
        return found.model_dump() if found else {"error": f"unknown candidate {candidate_label}"}

    def get_turns(candidate_label: str | None = None) -> list[dict[str, Any]]:
        """Tracked turns with who interrupted whom and who replied to whom."""
        # Each turn with its duration and interruption / reply links (first 60).
        return [
            {
                "speaker": t.candidate_label,
                "at": _clock(t.start),
                "seconds": round(t.end - t.start, 1),
                "interrupted": t.interrupted_label,
                "replied_to": t.reply_to_label,
            }
            for t in turns
            if candidate_label is None or t.candidate_label == candidate_label
        ][:60]

    def get_interactions(candidate_label: str | None = None) -> list[dict[str, Any]]:
        """Tool: who responded to whom, optionally limited to one candidate."""
        # Interaction edges involving the candidate (as sender or receiver), or all of them.
        return [
            {"from": e.source, "to": e.target, "count": e.count}
            for e in metrics.interactions
            if candidate_label in (None, e.source, e.target)
        ]

    def find_quotes(keyword: str, candidate_label: str | None = None) -> list[dict[str, Any]]:
        """Tool: transcript lines containing a keyword, to quote as evidence."""
        needle = keyword.lower()
        # Case-insensitive substring match, capped at 15 results.
        return [
            {"speaker": s.candidate_id, "at": _clock(s.start), "text": s.text}
            for s in segments
            if needle in s.text.lower()
            and (candidate_label is None or s.candidate_id == candidate_label)
        ][:15]

    # Describe each tool (name, purpose, argument schema) so the model knows how to call it.
    return [
        ToolDefinition(
            name="get_transcript_range",
            description="Read what was said between two times (seconds), optionally by one person.",
            input_schema={
                "type": "object",
                "required": ["start_seconds", "end_seconds"],
                "properties": {
                    "start_seconds": {"type": "number", "minimum": 0},
                    "end_seconds": {"type": "number", "minimum": 0},
                    "candidate_label": _LABEL,
                },
            },
            handler=get_transcript_range,
        ),
        ToolDefinition(
            name="get_candidate_metrics",
            description="Measured speaking time, turns, interruptions, responses for a candidate.",
            input_schema={
                "type": "object",
                "required": ["candidate_label"],
                "properties": {"candidate_label": _LABEL},
            },
            handler=get_candidate_metrics,
        ),
        ToolDefinition(
            name="get_turns",
            description="Tracked turns: who interrupted whom and who replied to whom, with times.",
            input_schema={"type": "object", "properties": {"candidate_label": _LABEL}},
            handler=get_turns,
        ),
        ToolDefinition(
            name="get_interactions",
            description="Who responded to or addressed whom, and how often.",
            input_schema={"type": "object", "properties": {"candidate_label": _LABEL}},
            handler=get_interactions,
        ),
        ToolDefinition(
            name="find_quotes",
            description="Find exact quotes containing a keyword, to cite as evidence.",
            input_schema={
                "type": "object",
                "required": ["keyword"],
                "properties": {"keyword": {"type": "string"}, "candidate_label": _LABEL},
            },
            handler=find_quotes,
        ),
    ]


def audit_evidence(output: EvaluationOutput, segments: list[TranscriptSegment]) -> list[str]:
    """Critic check: does each cited moment match something the candidate said?

    Returns human-readable problems (empty = clean). A score with no evidence at
    all is also a problem — the contract says every score carries evidence.
    """
    # Every score must cite at least one moment.
    if not output.evidence:
        return ["no evidence was cited for this score"]
    # Only this candidate's own speech counts as support for their score.
    mine = [s for s in segments if s.candidate_id == output.candidate_id]
    issues: list[str] = []
    # A confident verdict resting on a single moment is stronger than its evidence.
    if output.confidence >= OVERCONFIDENT_AT and len(output.evidence) < MIN_EVIDENCE_FOR_CONFIDENCE:
        issues.append(
            f"confidence {output.confidence:.2f} rests on {len(output.evidence)} cited moment(s)"
        )
    spoken = _norm(" ".join(s.text for s in mine))
    for item in output.evidence:
        # A quoted phrase must really be something this candidate said.
        for quote in re.findall(r'["“]([^"”]+)["”]', item.summary):
            if len(quote.split()) >= MIN_QUOTE_WORDS and _norm(quote) not in spoken:
                issues.append(f'quote not found in {output.candidate_id}\'s speech: "{quote[:60]}"')
        at = parse_timestamp(item.timestamp)
        # The cited time must parse...
        if at is None:
            issues.append(f"evidence timestamp {item.timestamp!r} is not mm:ss")
        # ...and fall within (a little slack of) a segment where they actually spoke.
        elif not any(
            s.start - TIMESTAMP_TOLERANCE_SECONDS <= at <= s.end + TIMESTAMP_TOLERANCE_SECONDS
            for s in mine
        ):
            issues.append(
                f"at {item.timestamp} candidate {output.candidate_id} was not speaking "
                f'("{item.summary[:60]}")'
            )
    return issues
