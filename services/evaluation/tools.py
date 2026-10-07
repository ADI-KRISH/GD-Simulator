"""Evidence tools for the agentic evaluators, and the critic's audit.

The specialists no longer score from a prompt alone: they can look things up.
Every handler here is read-only and answers from data already persisted in code
(transcript segments, deterministic metrics, tracked turns) — so a tool can never
hand the model a number the metrics engine did not compute (claude.md invariant 4).

`audit_evidence` is the critic's deterministic half: it checks that each piece of
cited evidence points at a moment the candidate actually spoke.
"""

import re
from dataclasses import dataclass
from typing import Any

from services.common.contracts import EvaluationOutput, TranscriptSegment
from services.llm.base import ToolDefinition
from services.metrics.engine import SessionMetrics

# Cited timestamps may be a little off the segment boundaries (mm:ss rounding).
TIMESTAMP_TOLERANCE_SECONDS = 15.0

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
    hours, minutes, seconds = (int(g or 0) for g in match.groups())
    return hours * 3600 + minutes * 60 + seconds


def _clock(seconds: float) -> str:
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
        return [
            {"speaker": s.candidate_id, "at": _clock(s.start), "text": s.text}
            for s in sorted(segments, key=lambda s: s.start)
            if s.end >= start_seconds
            and s.start <= end_seconds
            and (candidate_label is None or s.candidate_id == candidate_label)
        ][:40]

    def get_candidate_metrics(candidate_label: str) -> dict[str, Any]:
        found = metrics.per_candidate.get(candidate_label)
        return found.model_dump() if found else {"error": f"unknown candidate {candidate_label}"}

    def get_turns(candidate_label: str | None = None) -> list[dict[str, Any]]:
        """Tracked turns with who interrupted whom and who replied to whom."""
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
        return [
            {"from": e.source, "to": e.target, "count": e.count}
            for e in metrics.interactions
            if candidate_label in (None, e.source, e.target)
        ]

    def find_quotes(keyword: str, candidate_label: str | None = None) -> list[dict[str, Any]]:
        needle = keyword.lower()
        return [
            {"speaker": s.candidate_id, "at": _clock(s.start), "text": s.text}
            for s in segments
            if needle in s.text.lower()
            and (candidate_label is None or s.candidate_id == candidate_label)
        ][:15]

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
    if not output.evidence:
        return ["no evidence was cited for this score"]
    mine = [s for s in segments if s.candidate_id == output.candidate_id]
    issues: list[str] = []
    for item in output.evidence:
        at = parse_timestamp(item.timestamp)
        if at is None:
            issues.append(f"evidence timestamp {item.timestamp!r} is not mm:ss")
        elif not any(
            s.start - TIMESTAMP_TOLERANCE_SECONDS <= at <= s.end + TIMESTAMP_TOLERANCE_SECONDS
            for s in mine
        ):
            issues.append(
                f"at {item.timestamp} candidate {output.candidate_id} was not speaking "
                f'("{item.summary[:60]}")'
            )
    return issues
