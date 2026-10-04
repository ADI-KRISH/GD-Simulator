"""Deterministic metrics engine (FR-18, design §17, §30).

Everything in this module is computed in code from speech events and transcript
segments. No LLM is involved, and none may be: claude.md invariant 4 makes
speaking time, participation, turn count, interruptions and response latency
measured quantities rather than estimated ones. That is also what makes them
reproducible — the same session always yields the same numbers.

The one judgement call here is what counts as a *response*: without an LLM,
"A responded to B" is inferred from adjacency (A spoke immediately after B) plus
optional explicit address ("B, I think..."). That is stated plainly rather than
dressed up, and the Collaboration and Leadership agents get the raw transcript
too, so a wrong inference is recoverable at evaluation time.
"""

import re
from collections import defaultdict
from dataclasses import dataclass, field

from services.common.contracts import CandidateMetrics, TranscriptSegment, validate_contract
from services.speech.diarization import SpeakerSpan, find_overlaps

# A reply that lands within this long of the previous speaker finishing is
# treated as a direct response to them (design §30 interaction graph).
RESPONSE_WINDOW_SECONDS = 8.0

# Gap after which a candidate speaking again counts as a new turn rather than a
# continuation of the same one.
TURN_GAP_SECONDS = 2.0

_QUESTION_RE = re.compile(r"\?")
# Deliberately conservative: these are counted as signals for the Behavior and
# Critical Thinking agents to weigh, not as scores in themselves.
_SUPPORT_RE = re.compile(
    r"\b(i agree|agreed|building on|to add to|exactly|that supports|good point|"
    r"as \w+ said|seconding)\b",
    re.IGNORECASE,
)
_CHALLENGE_RE = re.compile(
    r"\b(i disagree|however|but |on the contrary|that assumes|counterpoint|"
    r"i'm not sure that|the problem with)\b",
    re.IGNORECASE,
)
_INTRODUCE_RE = re.compile(
    r"\b(i think|in my view|consider|another angle|we should|the real issue|"
    r"what about|my point is)\b",
    re.IGNORECASE,
)


@dataclass(slots=True)
class InteractionEdge:
    """One directed edge of the interaction graph (design §30): `source`
    responded to or addressed `target`."""

    source: str
    target: str
    count: int = 0
    timestamps: list[float] = field(default_factory=list)


@dataclass(slots=True)
class SessionMetrics:
    """Everything the deterministic pipeline knows about a session."""

    session_id: str
    per_candidate: dict[str, CandidateMetrics]
    interactions: list[InteractionEdge]
    total_speaking_seconds: float
    turn_order: list[str]

    def edges_from(self, candidate: str) -> list[InteractionEdge]:
        return [e for e in self.interactions if e.source == candidate]

    def edges_to(self, candidate: str) -> list[InteractionEdge]:
        return [e for e in self.interactions if e.target == candidate]

    def dominant(self, threshold: float = 0.30) -> list[str]:
        """Candidates holding more than `threshold` of the floor (design §34)."""
        return sorted(
            (c for c, m in self.per_candidate.items() if m.participation_ratio > threshold),
            key=lambda c: -self.per_candidate[c].participation_ratio,
        )

    def under_participating(self, threshold: float = 0.10) -> list[str]:
        return sorted(
            (c for c, m in self.per_candidate.items() if m.participation_ratio < threshold),
            key=lambda c: self.per_candidate[c].participation_ratio,
        )


def _merge_turns(spans: list[SpeakerSpan], gap: float = TURN_GAP_SECONDS) -> list[SpeakerSpan]:
    """Collapse a candidate's consecutive spans separated by a short pause.

    Breathing between sentences is not a new turn; without this, turn counts
    would measure microphone gating rather than conversational behaviour.
    """
    merged: list[SpeakerSpan] = []
    for span in sorted(spans, key=lambda s: s.start):
        if (
            merged
            and merged[-1].candidate_label == span.candidate_label
            and span.start - merged[-1].end <= gap
        ):
            previous = merged.pop()
            merged.append(
                SpeakerSpan(
                    candidate_label=span.candidate_label,
                    start=previous.start,
                    end=max(previous.end, span.end),
                )
            )
        else:
            merged.append(span)
    return merged


def _text_by_candidate(segments: list[TranscriptSegment]) -> dict[str, list[TranscriptSegment]]:
    grouped: dict[str, list[TranscriptSegment]] = defaultdict(list)
    for segment in segments:
        grouped[segment.candidate_id].append(segment)
    return grouped


def _addressed_target(text: str, candidates: set[str]) -> str | None:
    """Explicit address, e.g. "B, I think that overstates it".

    Only matches a candidate label used as a vocative at the start of the
    utterance — matching labels anywhere would fire on ordinary letters.
    """
    match = re.match(r"^\s*([A-Z])\s*[,:]", text)
    if match and match.group(1) in candidates:
        return match.group(1)
    return None


def compute_metrics(
    session_id: str,
    spans: list[SpeakerSpan],
    segments: list[TranscriptSegment],
    candidates: list[str] | None = None,
) -> SessionMetrics:
    """The whole deterministic pipeline: speech events in, metrics out.

    `spans` come from `speech_events` (microphone activity) and `segments` from
    the transcript. Timing metrics use spans because they are the ground truth
    for who held the floor; content-shaped counts use segments because they are
    the only thing with words in them.
    """
    labels = sorted(
        set(candidates or [])
        | {s.candidate_label for s in spans}
        | {s.candidate_id for s in segments}
    )
    turns = _merge_turns(spans)
    by_candidate_text = _text_by_candidate(segments)

    speaking: dict[str, float] = {label: 0.0 for label in labels}
    turn_counts: dict[str, int] = {label: 0 for label in labels}
    for turn in turns:
        speaking[turn.candidate_label] = speaking.get(turn.candidate_label, 0.0) + turn.duration
        turn_counts[turn.candidate_label] = turn_counts.get(turn.candidate_label, 0) + 1

    total = sum(speaking.values())

    # Interruptions: the later starter of a genuine overlap is the interrupter.
    interruptions: dict[str, int] = {label: 0 for label in labels}
    for _first, second in find_overlaps(spans):
        interruptions[second.candidate_label] = interruptions.get(second.candidate_label, 0) + 1

    # Responses and the interaction graph (design §30).
    edges: dict[tuple[str, str], InteractionEdge] = {}
    responses: dict[str, int] = {label: 0 for label in labels}
    latencies: dict[str, list[float]] = {label: [] for label in labels}
    label_set = set(labels)

    ordered_turns = sorted(turns, key=lambda s: s.start)
    for index, turn in enumerate(ordered_turns):
        if index == 0:
            continue
        previous = ordered_turns[index - 1]
        if previous.candidate_label == turn.candidate_label:
            continue

        first_text = next(
            (
                s.text
                for s in sorted(
                    by_candidate_text.get(turn.candidate_label, []), key=lambda s: s.start
                )
                if s.start >= turn.start - 0.01
            ),
            "",
        )
        addressed = _addressed_target(first_text, label_set - {turn.candidate_label})
        gap = turn.start - previous.end
        target = addressed or (previous.candidate_label if gap <= RESPONSE_WINDOW_SECONDS else None)
        if target is None:
            continue

        responses[turn.candidate_label] = responses.get(turn.candidate_label, 0) + 1
        if gap >= 0:
            latencies[turn.candidate_label].append(gap)
        key = (turn.candidate_label, target)
        edge = edges.setdefault(key, InteractionEdge(source=key[0], target=key[1]))
        edge.count += 1
        edge.timestamps.append(turn.start)

    per_candidate: dict[str, CandidateMetrics] = {}
    for label in labels:
        texts = [s.text for s in by_candidate_text.get(label, [])]
        blob = " ".join(texts)
        latency_samples = latencies.get(label, [])
        metrics = CandidateMetrics(
            session_id=session_id,
            candidate_id=label,
            speaking_seconds=round(speaking.get(label, 0.0), 3),
            participation_ratio=round(speaking.get(label, 0.0) / total, 6) if total else 0.0,
            turns=turn_counts.get(label, 0),
            interruptions=interruptions.get(label, 0),
            responses=responses.get(label, 0),
            questions=len(_QUESTION_RE.findall(blob)),
            arguments_introduced=len(_INTRODUCE_RE.findall(blob)),
            arguments_supported=len(_SUPPORT_RE.findall(blob)),
            arguments_challenged=len(_CHALLENGE_RE.findall(blob)),
            response_latency_seconds=(
                round(sum(latency_samples) / len(latency_samples), 3) if latency_samples else 0.0
            ),
        )
        validate_contract(metrics)
        per_candidate[label] = metrics

    return SessionMetrics(
        session_id=session_id,
        per_candidate=per_candidate,
        interactions=sorted(edges.values(), key=lambda e: (e.source, e.target)),
        total_speaking_seconds=round(total, 3),
        turn_order=[t.candidate_label for t in ordered_turns],
    )
