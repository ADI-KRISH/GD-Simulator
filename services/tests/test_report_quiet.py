"""A quiet candidate's debrief encourages and points at real openings (M14 EC-14.1).

It used to open "Overall: 0/100", list five agents' versions of "did not
contribute", and tell someone who spoke for 0% that "your points landed".
"""

from services.common.contracts import CandidateMetrics, TranscriptSegment
from services.evaluation.aggregator import AggregatedScore
from services.evaluation.report import build_candidate_report, entry_moments

SEGMENTS = [
    TranscriptSegment(
        session_id="s", confidence=0.9, candidate_id="P3", start=4.0, end=9.0,
        text="We're here to discuss internships. Asha, what's your take first?",
    ),
    TranscriptSegment(
        session_id="s", confidence=0.9, candidate_id="P1", start=20.0, end=30.0,
        text="Internships favour students with contacts in big cities.",
    ),
    TranscriptSegment(
        session_id="s", confidence=0.9, candidate_id="P2", start=75.0, end=82.0,
        text="asha, do you agree with Arjun on that?",
    ),
    TranscriptSegment(
        session_id="s", confidence=0.9, candidate_id="A", start=90.0, end=95.0,
        text="Asha here, I mostly agree.",
    ),
]  # fmt: skip
NAMES = {"P1": "Arjun", "P2": "Bhavana", "P3": "Chitra", "A": "Asha Rao"}


def _metrics(turns: int, ratio: float) -> CandidateMetrics:
    return CandidateMetrics(
        session_id="s", candidate_id="A", speaking_seconds=ratio * 100, participation_ratio=ratio,
        turns=turns, interruptions=0, responses=0, questions=0,
    )  # fmt: skip


def _score() -> AggregatedScore:
    return AggregatedScore(
        candidate_label="A", overall=12.0, dimensions={"communication": 1.2},
        weights={"communication": 1.0}, confidence=0.5, needs_human_review=False,
    )  # fmt: skip


def test_entry_moments_are_the_lines_that_named_the_candidate():
    moments = entry_moments(SEGMENTS, "A", "Asha Rao", NAMES)
    assert [(m["timestamp"], m["speaker"]) for m in moments] == [
        ("0:04", "Chitra"),
        ("1:15", "Bhavana"),
    ]


def test_a_silent_candidates_debrief_is_about_getting_in():
    moments = entry_moments(SEGMENTS, "A", "Asha Rao", NAMES)
    report = build_candidate_report(
        "A", "Asha Rao", _score(), [], _metrics(0, 0.0), moments=moments
    )
    assert report.weaknesses[0].startswith("You didn't come into the discussion this time")
    assert (
        "At 0:04, Chitra turned to you: \"We're here to discuss internships."
        in report.weaknesses[1]
    )
    assert "Overall:" not in report.feedback
    assert "landed" not in report.feedback
    assert report.entry_moments == moments


def test_a_quiet_candidate_is_not_told_their_points_landed():
    report = build_candidate_report("A", "Asha Rao", _score(), [], _metrics(2, 0.05))
    joined = " ".join(report.weaknesses)
    assert "less than an even share" in joined
    assert "landed" not in joined
