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

Purpose: The pure computation that turns speech events and transcript segments into
    per-candidate metrics (speaking time, participation, turns, interruptions, responses,
    latency, question and argument counts) and an interaction graph.
Why needed: These numbers are evidence for scoring, so they must be measured, reproducible and
    free of any LLM (invariant 4). Being a pure function of its inputs makes the engine easy to
    unit-test and impossible to drift out of sync with the data.
Contains:
  - RESPONSE_WINDOW_SECONDS / TURN_GAP_SECONDS: thresholds for what counts as a reply and as a
    new turn.
  - _QUESTION_RE / _SUPPORT_RE / _CHALLENGE_RE / _INTRODUCE_RE: conservative text signals
    counted for the Behavior and Critical Thinking agents.
  - InteractionEdge: one directed who-responded-to-whom edge with counts and timestamps.
  - SessionMetrics: all results, plus helpers for edges, dominant speakers and
    under-participants.
  - _merge_turns: join a speaker's spans separated by a short pause into one turn.
  - _text_by_candidate: group transcript segments by speaker.
  - _addressed_target: detect an explicit B, ... address at the start of an utterance.
  - compute_metrics: the whole pipeline, from speech events in to validated metrics out.
Used by: services/evaluation/evidence.py, services/evaluation/graph.py,
    services/evaluation/group_synthesis.py, services/evaluation/report.py,
    services/evaluation/supervisor.py, services/evaluation/tools.py,
    services/metrics/service.py, services/session/service.py,
    services/tests/test_evaluation_batching.py, services/tests/test_group_synthesis.py,
    services/tests/test_langgraph_engine.py, services/tests/test_metrics_engine.py
Depends on: services/common/contracts.py, services/speech/diarization.py
"""

import re  # regex signals: questions, agreement, challenge, introducing ideas
from collections import defaultdict  # group transcript segments by candidate
from dataclasses import dataclass, field  # metric containers

from services.common.contracts import CandidateMetrics, TranscriptSegment, validate_contract
from services.speech.diarization import SpeakerSpan, find_overlaps  # who-spoke-when, overlaps

# A reply that lands within this long of the previous speaker finishing is
# treated as a direct response to them (design §30 interaction graph).
RESPONSE_WINDOW_SECONDS = 8.0

# Gap after which a candidate speaking again counts as a new turn rather than a
# continuation of the same one.
TURN_GAP_SECONDS = 2.0

# A question mark is the simplest reliable signal for a question.
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
        """Interaction edges where `candidate` is the one responding."""
        return [e for e in self.interactions if e.source == candidate]

    def edges_to(self, candidate: str) -> list[InteractionEdge]:
        """Interaction edges where `candidate` is the one being responded to."""
        return [e for e in self.interactions if e.target == candidate]

    def dominant(self, threshold: float = 0.30) -> list[str]:
        """Candidates holding more than `threshold` of the floor (design §34)."""
        # Highest share of the floor first.
        return sorted(
            (c for c, m in self.per_candidate.items() if m.participation_ratio > threshold),
            key=lambda c: -self.per_candidate[c].participation_ratio,
        )

    def under_participating(self, threshold: float = 0.10) -> list[str]:
        """Candidates holding less than `threshold` of the floor, quietest first."""
        # Lowest share of the floor first.
        return sorted(
            (c for c, m in self.per_candidate.items() if m.participation_ratio < threshold),
            key=lambda c: self.per_candidate[c].participation_ratio,
        )


def _merge_turns(spans: list[SpeakerSpan], gap: float = TURN_GAP_SECONDS) -> list[SpeakerSpan]:
    """Collapse a candidate's consecutive spans separated by a short pause.

    Breathing between sentences is not a new turn; without this, turn counts
    would measure microphone gating rather than conversational behaviour.
    """
    # Walk spans in time order, extending the previous span when the same speaker resumes within the
    # gap.
    merged: list[SpeakerSpan] = []
    for span in sorted(spans, key=lambda s: s.start):
        # Same speaker and a pause no longer than the gap: this is a continuation.
        if (
            merged
            and merged[-1].candidate_label == span.candidate_label
            and span.start - merged[-1].end <= gap
        ):
            # Replace the previous span with one covering both.
            previous = merged.pop()
            merged.append(
                SpeakerSpan(
                    candidate_label=span.candidate_label,
                    start=previous.start,
                    end=max(previous.end, span.end),
                )
            )
        # A different speaker or a long pause starts a new turn.
        else:
            merged.append(span)
    return merged


def _text_by_candidate(segments: list[TranscriptSegment]) -> dict[str, list[TranscriptSegment]]:
    """Group transcript segments by the candidate who said them."""
    grouped: dict[str, list[TranscriptSegment]] = defaultdict(list)
    for segment in segments:
        grouped[segment.candidate_id].append(segment)
    return grouped


def _addressed_target(text: str, candidates: set[str]) -> str | None:
    """Explicit address, e.g. "B, I think that overstates it".

    Only matches a candidate label used as a vocative at the start of the
    utterance — matching labels anywhere would fire on ordinary letters.
    """
    # A capital letter followed by a comma or colon at the very start, like B, ...
    match = re.match(r"^\s*([A-Z])\s*[,:]", text)
    # Only count it if that label is a real candidate.
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
    # Every candidate we know of, from the roster, speech events or transcript.
    labels = sorted(
        set(candidates or [])
        | {s.candidate_label for s in spans}
        | {s.candidate_id for s in segments}
    )
    # Merge pauses into single turns so turn counts reflect conversation, not microphone gating.
    turns = _merge_turns(spans)
    # Group the transcript once; reused for the word-based counts.
    by_candidate_text = _text_by_candidate(segments)

    # Speaking time and turn count per candidate, from the merged turns.
    speaking: dict[str, float] = {label: 0.0 for label in labels}
    turn_counts: dict[str, int] = {label: 0 for label in labels}
    for turn in turns:
        speaking[turn.candidate_label] = speaking.get(turn.candidate_label, 0.0) + turn.duration
        turn_counts[turn.candidate_label] = turn_counts.get(turn.candidate_label, 0) + 1

    # Total floor time, the denominator for participation ratios.
    total = sum(speaking.values())

    # Interruptions: the later starter of a genuine overlap is the interrupter.
    interruptions: dict[str, int] = {label: 0 for label in labels}
    # Each genuine overlap credits the later starter with one interruption.
    for _first, second in find_overlaps(spans):
        interruptions[second.candidate_label] = interruptions.get(second.candidate_label, 0) + 1

    # Responses and the interaction graph (design §30).
    edges: dict[tuple[str, str], InteractionEdge] = {}
    responses: dict[str, int] = {label: 0 for label in labels}
    latencies: dict[str, list[float]] = {label: [] for label in labels}
    label_set = set(labels)

    # Walk turns in time order so each one can be compared with the turn before it.
    ordered_turns = sorted(turns, key=lambda s: s.start)
    for index, turn in enumerate(ordered_turns):
        if index == 0:
            continue  # the first turn responds to nobody
        previous = ordered_turns[index - 1]
        if previous.candidate_label == turn.candidate_label:
            continue  # same speaker continuing is not a response

        # The words that open this turn, used to spot an explicit "B, ..." address.
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
        # An explicit address to a candidate beats simple adjacency.
        addressed = _addressed_target(first_text, label_set - {turn.candidate_label})
        # Silence between the previous speaker finishing and this one starting (negative means they
        # overlapped).
        gap = turn.start - previous.end
        # Respond to whoever was addressed, otherwise to the previous speaker if they spoke within
        # the response window.
        target = addressed or (previous.candidate_label if gap <= RESPONSE_WINDOW_SECONDS else None)
        # Too long a gap and no address: not a response to anyone.
        if target is None:
            continue

        # Count the response and record its latency; overlaps (negative gaps) have no latency.
        responses[turn.candidate_label] = responses.get(turn.candidate_label, 0) + 1
        if gap >= 0:
            latencies[turn.candidate_label].append(gap)
        # One edge per (responder, target) pair, accumulating counts and timestamps.
        key = (turn.candidate_label, target)
        edge = edges.setdefault(key, InteractionEdge(source=key[0], target=key[1]))
        edge.count += 1
        edge.timestamps.append(turn.start)

    # Assemble one validated CandidateMetrics per candidate (text counts from regexes).
    per_candidate: dict[str, CandidateMetrics] = {}
    for label in labels:
        # Word-based signals use only this candidate's transcript text.
        texts = [s.text for s in by_candidate_text.get(label, [])]
        blob = " ".join(texts)
        latency_samples = latencies.get(label, [])
        # Ratios and averages are rounded for stable, reproducible output.
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
        # Fail loudly if the numbers do not fit the contract.
        validate_contract(metrics)
        per_candidate[label] = metrics

    # Edges are sorted so the same session always produces identical output.
    return SessionMetrics(
        session_id=session_id,
        per_candidate=per_candidate,
        interactions=sorted(edges.values(), key=lambda e: (e.source, e.target)),
        total_speaking_seconds=round(total, 3),
        turn_order=[t.candidate_label for t in ordered_turns],
    )
