"""Deterministic metrics math (design §17, §30, claude.md invariant 4).

Every number the evaluation pipeline treats as fact is computed here, so these
are hand-worked examples with the expected answer written out — if the engine
drifts, a test fails rather than a candidate quietly getting the wrong score.

Purpose: Unit tests of the deterministic metrics engine using hand-worked examples with the
    expected numbers written out.
Why needed: Every number the evaluation treats as fact comes from this engine, so a drift in the
    maths must fail a test instead of silently giving a candidate the wrong score (invariant 4).
Contains:
  - span / seg: helpers that build speaker spans and transcript segments.
  - Speaking time tests: exact shares, a silent candidate at zero, no speech without a division
    error.
  - Turn tests: short pauses merge into one turn, long gaps start a new one.
  - Interruption tests: the later starter is the interrupter, brief hand-offs and self-overlap
    do not count, speaking order collapses repeats.
  - Response tests: a prompt reply is a response with latency, a long silence breaks the link,
    an explicit address beats adjacency.
  - Transcript signal tests: question and argument counts come from the text.
  - Dominance, reproducibility and contract-validity tests.
Used by: nothing imports it directly (run by pytest, which discovers test_*.py files)
Depends on: services/common/contracts.py, services/metrics/engine.py,
    services/speech/diarization.py
"""

import pytest

from services.common.contracts import TranscriptSegment
from services.metrics.engine import (
    RESPONSE_WINDOW_SECONDS,
    TURN_GAP_SECONDS,
    compute_metrics,
)
from services.speech.diarization import (
    MIN_OVERLAP_SECONDS,
    SpeakerSpan,
    find_overlaps,
    speaking_order,
)

SESSION = "GD123"


def span(label: str, start: float, end: float) -> SpeakerSpan:
    """Build a speaker span."""
    return SpeakerSpan(candidate_label=label, start=start, end=end)


def seg(label: str, start: float, end: float, text: str) -> TranscriptSegment:
    """Build a transcript segment."""
    return TranscriptSegment(
        session_id=SESSION, candidate_id=label, start=start, end=end, text=text, confidence=0.9
    )


def test_speaking_time_and_participation_are_exact():
    """A: 60s, B: 30s, C: 10s → 60%, 30%, 10% of a 100s floor."""
    # Three speakers back to back: 60s, 30s and 10s of a 100s floor.
    spans = [span("A", 0, 60), span("B", 60, 90), span("C", 90, 100)]
    # No transcript is needed: timing metrics come from the spans alone.
    metrics = compute_metrics(SESSION, spans, [], ["A", "B", "C"])

    # Totals and shares must be exact, not approximate estimates.
    assert metrics.total_speaking_seconds == 100.0
    assert metrics.per_candidate["A"].speaking_seconds == 60.0
    assert metrics.per_candidate["A"].participation_ratio == pytest.approx(0.6)
    assert metrics.per_candidate["B"].participation_ratio == pytest.approx(0.3)
    assert metrics.per_candidate["C"].participation_ratio == pytest.approx(0.1)
    # The shares always add up to 100%.
    assert sum(m.participation_ratio for m in metrics.per_candidate.values()) == pytest.approx(1.0)


def test_a_silent_candidate_gets_zeroes_not_an_error():
    """A candidate who never speaks must still appear, at zero."""
    # B is on the roster but never speaks.
    metrics = compute_metrics(SESSION, [span("A", 0, 30)], [], ["A", "B"])
    # B still appears, with zeros everywhere.
    assert metrics.per_candidate["B"].speaking_seconds == 0.0
    assert metrics.per_candidate["B"].participation_ratio == 0.0
    assert metrics.per_candidate["B"].turns == 0


def test_no_speech_at_all_does_not_divide_by_zero():
    """With no speech, ratios are 0 rather than a division error."""
    # No spans at all: the total is zero.
    metrics = compute_metrics(SESSION, [], [], ["A", "B"])
    assert metrics.total_speaking_seconds == 0.0
    # Ratios fall back to 0.0 rather than dividing by zero.
    assert all(m.participation_ratio == 0.0 for m in metrics.per_candidate.values())


def test_short_pauses_do_not_inflate_turn_count():
    """Breathing between sentences is one turn, not three."""
    # Three bursts one second apart, shorter than TURN_GAP_SECONDS.
    spans = [span("A", 0, 5), span("A", 6, 10), span("A", 11, 15)]
    metrics = compute_metrics(SESSION, spans, [], ["A"])
    # They merge into a single turn.
    assert metrics.per_candidate["A"].turns == 1
    # The merged turn spans 0→15, including the gaps it swallowed.
    assert metrics.per_candidate["A"].speaking_seconds == 15.0


def test_a_long_gap_starts_a_new_turn():
    """A pause longer than the turn gap counts as a new turn."""
    # A pause just over the turn-gap threshold.
    gap = TURN_GAP_SECONDS + 1
    spans = [span("A", 0, 5), span("A", 5 + gap, 10 + gap)]
    metrics = compute_metrics(SESSION, spans, [], ["A"])
    # Now it counts as two turns.
    assert metrics.per_candidate["A"].turns == 2


def test_interruption_is_attributed_to_the_later_speaker():
    """B starts while A is still talking → B interrupted A, not the reverse."""
    # B starts at 8s while A (0 to 10s) still holds the floor.
    spans = [span("A", 0, 10), span("B", 8, 15)]
    metrics = compute_metrics(SESSION, spans, [], ["A", "B"])
    # The later starter takes the interruption.
    assert metrics.per_candidate["B"].interruptions == 1
    assert metrics.per_candidate["A"].interruptions == 0


def test_a_brief_handoff_is_not_an_interruption():
    """Overlap under the threshold is ordinary conversational overlap."""
    # An overlap below the minimum.
    overlap = MIN_OVERLAP_SECONDS / 2
    # B begins fractionally before A finishes.
    spans = [span("A", 0, 10), span("B", 10 - overlap, 15)]
    metrics = compute_metrics(SESSION, spans, [], ["A", "B"])
    # Below the threshold, so no interruption is counted.
    assert metrics.per_candidate["B"].interruptions == 0


def test_the_same_candidate_cannot_interrupt_themselves():
    """Overlapping spans from one candidate are not an interruption."""
    # A overlaps A.
    spans = [span("A", 0, 10), span("A", 5, 15)]
    # No overlap is reported between a speaker and themselves.
    assert find_overlaps(spans) == []


def test_speaking_order_collapses_consecutive_repeats():
    """Consecutive turns by the same candidate collapse in the speaking order."""
    # A speaks twice in a row, then B, then A again.
    spans = [span("A", 0, 5), span("A", 10, 15), span("B", 20, 25), span("A", 30, 35)]
    # The repeated A turns collapse into one.
    assert speaking_order(spans) == ["A", "B", "A"]


def test_a_prompt_reply_counts_as_a_response():
    """B speaking right after A is a response to A (design §30)."""
    # B starts one second after A finishes.
    spans = [span("A", 0, 10), span("B", 11, 20)]
    metrics = compute_metrics(SESSION, spans, [], ["A", "B"])
    # That counts as a response with a 1.0s latency and a B to A edge.
    assert metrics.per_candidate["B"].responses == 1
    assert metrics.per_candidate["B"].response_latency_seconds == pytest.approx(1.0)
    assert [(e.source, e.target, e.count) for e in metrics.interactions] == [("B", "A", 1)]


def test_a_long_silence_breaks_the_response_link():
    """Speaking a minute later is a new thought, not a reply."""
    # B waits beyond the response window.
    late = 10 + RESPONSE_WINDOW_SECONDS + 5
    spans = [span("A", 0, 10), span("B", late, late + 5)]
    metrics = compute_metrics(SESSION, spans, [], ["A", "B"])
    # No response is recorded and no interaction edge exists.
    assert metrics.per_candidate["B"].responses == 0
    assert metrics.interactions == []


def test_explicit_address_beats_adjacency():
    """ "C, that assumes..." addresses C even though B spoke last."""
    # A, then C, then B thirty seconds later.
    spans = [span("A", 0, 5), span("C", 6, 10), span("B", 40, 50)]
    # B's words start with an explicit address to C.
    segments = [
        seg("A", 0, 5, "Regulation will slow innovation."),
        seg("C", 6, 10, "I am not sure that follows."),
        seg("B", 40, 50, "C, that assumes the cost falls on startups."),
    ]
    metrics = compute_metrics(SESSION, spans, segments, ["A", "B", "C"])
    # All (source, target) pairs found.
    edges = {(e.source, e.target) for e in metrics.interactions}
    # B addressed C explicitly, despite the 30s gap that would otherwise break
    # the adjacency link.
    assert ("B", "C") in edges


def test_question_and_argument_counts_come_from_the_transcript():
    """Question and argument counts are derived from the transcript text."""
    # Texts chosen to trigger each signal: a question, an introduction, a challenge and support.
    segments = [
        seg("A", 0, 5, "Who actually bears that cost?"),
        seg("A", 6, 10, "I think the burden lands on small firms."),
        seg("B", 11, 15, "I disagree, the data says otherwise."),
        seg("B", 16, 20, "I agree with A on the second point though."),
    ]
    # Timing so each speaker's segments are attributed correctly.
    spans = [span("A", 0, 10), span("B", 11, 20)]
    metrics = compute_metrics(SESSION, spans, segments, ["A", "B"])

    # Counts come from the transcript text alone, using the engine's regex signals.
    assert metrics.per_candidate["A"].questions == 1
    assert metrics.per_candidate["B"].questions == 0
    assert metrics.per_candidate["A"].arguments_introduced >= 1
    assert metrics.per_candidate["B"].arguments_challenged >= 1
    assert metrics.per_candidate["B"].arguments_supported >= 1


def test_dominant_and_under_participating_are_derived_from_share():
    """Design §34 group report inputs."""
    # A holds 70% of the floor, B 25% and C 5%.
    spans = [span("A", 0, 70), span("B", 70, 95), span("C", 95, 100)]
    metrics = compute_metrics(SESSION, spans, [], ["A", "B", "C"])
    # Over 30% is dominant; under 10% is under-participating.
    assert metrics.dominant(0.30) == ["A"]
    assert metrics.under_participating(0.10) == ["C"]


def test_metrics_are_reproducible():
    """Same input, same numbers — the point of computing them in code."""
    # A mix of overlaps and gaps.
    spans = [span("A", 0, 12), span("B", 11, 20), span("A", 25, 30)]
    segments = [seg("A", 0, 12, "Why would that be true?")]
    # Compute twice from identical input.
    first = compute_metrics(SESSION, spans, segments, ["A", "B"])
    second = compute_metrics(SESSION, spans, segments, ["A", "B"])
    # The results must be identical, which is the reason metrics are computed in code.
    assert first.per_candidate["A"].model_dump() == second.per_candidate["A"].model_dump()
    assert first.per_candidate["B"].model_dump() == second.per_candidate["B"].model_dump()


def test_every_candidate_metric_satisfies_its_contract():
    """`compute_metrics` validates against candidate_metrics.schema.json, so a
    drift between the engine and the contract fails here."""
    # A minimal two-speaker session.
    spans = [span("A", 0, 30), span("B", 30, 45)]
    metrics = compute_metrics(SESSION, spans, [seg("A", 0, 30, "Point?")], ["A", "B"])
    # Check identity fields and bounds.
    for label, candidate in metrics.per_candidate.items():
        assert candidate.candidate_id == label
        assert candidate.session_id == SESSION
        assert 0.0 <= candidate.participation_ratio <= 1.0
