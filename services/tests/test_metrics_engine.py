"""Deterministic metrics math (design §17, §30, claude.md invariant 4).

Every number the evaluation pipeline treats as fact is computed here, so these
are hand-worked examples with the expected answer written out — if the engine
drifts, a test fails rather than a candidate quietly getting the wrong score.
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
    return SpeakerSpan(candidate_label=label, start=start, end=end)


def seg(label: str, start: float, end: float, text: str) -> TranscriptSegment:
    return TranscriptSegment(
        session_id=SESSION, candidate_id=label, start=start, end=end, text=text, confidence=0.9
    )


def test_speaking_time_and_participation_are_exact():
    """A: 60s, B: 30s, C: 10s → 60%, 30%, 10% of a 100s floor."""
    spans = [span("A", 0, 60), span("B", 60, 90), span("C", 90, 100)]
    metrics = compute_metrics(SESSION, spans, [], ["A", "B", "C"])

    assert metrics.total_speaking_seconds == 100.0
    assert metrics.per_candidate["A"].speaking_seconds == 60.0
    assert metrics.per_candidate["A"].participation_ratio == pytest.approx(0.6)
    assert metrics.per_candidate["B"].participation_ratio == pytest.approx(0.3)
    assert metrics.per_candidate["C"].participation_ratio == pytest.approx(0.1)
    assert sum(m.participation_ratio for m in metrics.per_candidate.values()) == pytest.approx(1.0)


def test_a_silent_candidate_gets_zeroes_not_an_error():
    """A candidate who never speaks must still appear, at zero."""
    metrics = compute_metrics(SESSION, [span("A", 0, 30)], [], ["A", "B"])
    assert metrics.per_candidate["B"].speaking_seconds == 0.0
    assert metrics.per_candidate["B"].participation_ratio == 0.0
    assert metrics.per_candidate["B"].turns == 0


def test_no_speech_at_all_does_not_divide_by_zero():
    metrics = compute_metrics(SESSION, [], [], ["A", "B"])
    assert metrics.total_speaking_seconds == 0.0
    assert all(m.participation_ratio == 0.0 for m in metrics.per_candidate.values())


def test_short_pauses_do_not_inflate_turn_count():
    """Breathing between sentences is one turn, not three."""
    spans = [span("A", 0, 5), span("A", 6, 10), span("A", 11, 15)]
    metrics = compute_metrics(SESSION, spans, [], ["A"])
    assert metrics.per_candidate["A"].turns == 1
    # The merged turn spans 0→15, including the gaps it swallowed.
    assert metrics.per_candidate["A"].speaking_seconds == 15.0


def test_a_long_gap_starts_a_new_turn():
    gap = TURN_GAP_SECONDS + 1
    spans = [span("A", 0, 5), span("A", 5 + gap, 10 + gap)]
    metrics = compute_metrics(SESSION, spans, [], ["A"])
    assert metrics.per_candidate["A"].turns == 2


def test_interruption_is_attributed_to_the_later_speaker():
    """B starts while A is still talking → B interrupted A, not the reverse."""
    spans = [span("A", 0, 10), span("B", 8, 15)]
    metrics = compute_metrics(SESSION, spans, [], ["A", "B"])
    assert metrics.per_candidate["B"].interruptions == 1
    assert metrics.per_candidate["A"].interruptions == 0


def test_a_brief_handoff_is_not_an_interruption():
    """Overlap under the threshold is ordinary conversational overlap."""
    overlap = MIN_OVERLAP_SECONDS / 2
    spans = [span("A", 0, 10), span("B", 10 - overlap, 15)]
    metrics = compute_metrics(SESSION, spans, [], ["A", "B"])
    assert metrics.per_candidate["B"].interruptions == 0


def test_the_same_candidate_cannot_interrupt_themselves():
    spans = [span("A", 0, 10), span("A", 5, 15)]
    assert find_overlaps(spans) == []


def test_speaking_order_collapses_consecutive_repeats():
    spans = [span("A", 0, 5), span("A", 10, 15), span("B", 20, 25), span("A", 30, 35)]
    assert speaking_order(spans) == ["A", "B", "A"]


def test_a_prompt_reply_counts_as_a_response():
    """B speaking right after A is a response to A (design §30)."""
    spans = [span("A", 0, 10), span("B", 11, 20)]
    metrics = compute_metrics(SESSION, spans, [], ["A", "B"])
    assert metrics.per_candidate["B"].responses == 1
    assert metrics.per_candidate["B"].response_latency_seconds == pytest.approx(1.0)
    assert [(e.source, e.target, e.count) for e in metrics.interactions] == [("B", "A", 1)]


def test_a_long_silence_breaks_the_response_link():
    """Speaking a minute later is a new thought, not a reply."""
    late = 10 + RESPONSE_WINDOW_SECONDS + 5
    spans = [span("A", 0, 10), span("B", late, late + 5)]
    metrics = compute_metrics(SESSION, spans, [], ["A", "B"])
    assert metrics.per_candidate["B"].responses == 0
    assert metrics.interactions == []


def test_explicit_address_beats_adjacency():
    """ "C, that assumes..." addresses C even though B spoke last."""
    spans = [span("A", 0, 5), span("C", 6, 10), span("B", 40, 50)]
    segments = [
        seg("A", 0, 5, "Regulation will slow innovation."),
        seg("C", 6, 10, "I am not sure that follows."),
        seg("B", 40, 50, "C, that assumes the cost falls on startups."),
    ]
    metrics = compute_metrics(SESSION, spans, segments, ["A", "B", "C"])
    edges = {(e.source, e.target) for e in metrics.interactions}
    # B addressed C explicitly, despite the 30s gap that would otherwise break
    # the adjacency link.
    assert ("B", "C") in edges


def test_question_and_argument_counts_come_from_the_transcript():
    segments = [
        seg("A", 0, 5, "Who actually bears that cost?"),
        seg("A", 6, 10, "I think the burden lands on small firms."),
        seg("B", 11, 15, "I disagree, the data says otherwise."),
        seg("B", 16, 20, "I agree with A on the second point though."),
    ]
    spans = [span("A", 0, 10), span("B", 11, 20)]
    metrics = compute_metrics(SESSION, spans, segments, ["A", "B"])

    assert metrics.per_candidate["A"].questions == 1
    assert metrics.per_candidate["B"].questions == 0
    assert metrics.per_candidate["A"].arguments_introduced >= 1
    assert metrics.per_candidate["B"].arguments_challenged >= 1
    assert metrics.per_candidate["B"].arguments_supported >= 1


def test_dominant_and_under_participating_are_derived_from_share():
    """Design §34 group report inputs."""
    spans = [span("A", 0, 70), span("B", 70, 95), span("C", 95, 100)]
    metrics = compute_metrics(SESSION, spans, [], ["A", "B", "C"])
    assert metrics.dominant(0.30) == ["A"]
    assert metrics.under_participating(0.10) == ["C"]


def test_metrics_are_reproducible():
    """Same input, same numbers — the point of computing them in code."""
    spans = [span("A", 0, 12), span("B", 11, 20), span("A", 25, 30)]
    segments = [seg("A", 0, 12, "Why would that be true?")]
    first = compute_metrics(SESSION, spans, segments, ["A", "B"])
    second = compute_metrics(SESSION, spans, segments, ["A", "B"])
    assert first.per_candidate["A"].model_dump() == second.per_candidate["A"].model_dump()
    assert first.per_candidate["B"].model_dump() == second.per_candidate["B"].model_dump()


def test_every_candidate_metric_satisfies_its_contract():
    """`compute_metrics` validates against candidate_metrics.schema.json, so a
    drift between the engine and the contract fails here."""
    spans = [span("A", 0, 30), span("B", 30, 45)]
    metrics = compute_metrics(SESSION, spans, [seg("A", 0, 30, "Point?")], ["A", "B"])
    for label, candidate in metrics.per_candidate.items():
        assert candidate.candidate_id == label
        assert candidate.session_id == SESSION
        assert 0.0 <= candidate.participation_ratio <= 1.0
