"""Speaker attribution (FR-14, design §11, §12).

Design §12 is emphatic that "who spoke, when, for how long, in what order" is
what makes the evaluation possible at all. There are two ways to get it, and
this module makes the choice explicit rather than implicit:

**Speaker mapping (the path this system uses).** Every candidate publishes their
own microphone track through LiveKit, and the browser uploads that track's audio
under the candidate's own label. Attribution is therefore exact by construction
— no clustering, no error rate, no confusion between similar voices. Design §11
names this "Speaker Diarization / Speaker Mapping"; when per-speaker streams are
available, mapping is strictly better than diarizing a mixed stream.

**Diarization (the fallback).** If a single mixed stream ever arrives — a room
mic, a recording imported after the fact — attribution has to be inferred. The
`Diarizer` protocol exists for that case so the rest of the pipeline does not
have to care which path produced a segment.

Overlap detection stays here either way: it is the same computation regardless
of how speakers were attributed, and it is what "interruption" is built from.

Purpose: Speaker attribution helpers: speaker spans, conversion of chunk-relative times to the
    session clock, overlap (interruption) detection and turn order.
Why needed: Who spoke, when and for how long is what makes evaluation possible (design §12).
    Each browser records only its own microphone, so attribution is exact; this module keeps
    that mapping, the overlap maths and a fallback interface for mixed audio in one place.
Contains:
  - MIN_OVERLAP_SECONDS: overlap needed to count as an interruption rather than a hand-off.
  - SpeakerSpan: a stretch of time attributed to one candidate, with duration and overlap_with.
  - Diarizer: protocol for a fallback diarizer on mixed-stream audio.
  - map_to_session_clock: shift STT chunk times onto the session clock.
  - find_overlaps: pairs of different speakers who genuinely overlap, later starter second.
  - speaking_order: turn order with consecutive repeats collapsed.
Used by: services/metrics/engine.py, services/metrics/service.py, services/session/service.py,
    services/speech/service.py, services/tests/test_evaluation_batching.py,
    services/tests/test_langgraph_engine.py, services/tests/test_metrics_engine.py
Depends on: nothing inside the project (standard library / third-party only)
"""

from dataclasses import dataclass  # immutable SpeakerSpan
from typing import Protocol  # structural interface for fallback diarizers

# Two speakers must overlap by more than this to count as a real interruption
# rather than ordinary conversational hand-off (design §17, §29).
MIN_OVERLAP_SECONDS = 0.5


@dataclass(frozen=True, slots=True)
class SpeakerSpan:
    """A stretch of time attributed to one candidate."""

    candidate_label: str
    start: float
    end: float

    @property
    def duration(self) -> float:
        """Length of the span in seconds (never negative)."""
        return max(0.0, self.end - self.start)

    def overlap_with(self, other: "SpeakerSpan") -> float:
        """Seconds this span overlaps `other` (0 if they do not touch)."""
        return max(0.0, min(self.end, other.end) - max(self.start, other.start))


class Diarizer(Protocol):
    """Fallback interface for mixed-stream audio."""

    name: str

    async def diarize(self, audio: bytes, *, offset: float = 0.0) -> list[SpeakerSpan]:
        """Split mixed audio into per-speaker spans, shifted by `offset` seconds."""
        ...


def map_to_session_clock(
    candidate_label: str, spans: list[tuple[float, float]], chunk_start: float
) -> list[SpeakerSpan]:
    """Shift chunk-relative spans onto the session clock.

    STT reports times relative to the audio it was given; everything downstream
    — transcript, metrics, evidence timestamps — works in seconds since the
    session went LIVE. This is the single place that conversion happens.
    """
    # Add the chunk's start offset to each span's relative times.
    return [
        SpeakerSpan(
            candidate_label=candidate_label, start=chunk_start + start, end=chunk_start + end
        )
        for start, end in spans
    ]


def find_overlaps(spans: list[SpeakerSpan]) -> list[tuple[SpeakerSpan, SpeakerSpan]]:
    """Pairs of spans from different speakers that genuinely overlap.

    Ordered so the *later* starter is second: that is the one doing the
    interrupting. Two spans from the same candidate are never an interruption.
    """
    # Sort by start so the later starter is always second in a pair.
    ordered = sorted(spans, key=lambda s: s.start)
    overlaps: list[tuple[SpeakerSpan, SpeakerSpan]] = []
    # Compare each span only with the ones that start after it.
    for i, first in enumerate(ordered):
        for second in ordered[i + 1 :]:
            if second.start >= first.end:
                # Sorted by start, so nothing later can overlap this one either.
                break
            # Overlapping yourself is not an interruption.
            if second.candidate_label == first.candidate_label:
                continue
            # Ignore tiny overlaps that are just conversational hand-off.
            if first.overlap_with(second) > MIN_OVERLAP_SECONDS:
                overlaps.append((first, second))
    return overlaps


def speaking_order(spans: list[SpeakerSpan]) -> list[str]:
    """Turn order with consecutive repeats collapsed.

    A candidate pausing and resuming is one turn, not two — which is what
    "in what order" (design §12) actually means.
    """
    # Collapse consecutive repeats so a pause and resume counts as one turn.
    order: list[str] = []
    for span in sorted(spans, key=lambda s: s.start):
        if not order or order[-1] != span.candidate_label:
            order.append(span.candidate_label)
    return order
