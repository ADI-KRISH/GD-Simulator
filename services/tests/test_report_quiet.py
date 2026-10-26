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
    """Metrics for candidate A with the given turns and participation ratio."""
    return CandidateMetrics(
        session_id="s", candidate_id="A", speaking_seconds=ratio * 100, participation_ratio=ratio,
        turns=turns, interruptions=0, responses=0, questions=0,
    )  # fmt: skip


def _score() -> AggregatedScore:
    """A fixed low score for candidate A."""
    return AggregatedScore(
        candidate_label="A", overall=12.0, dimensions={"communication": 1.2},
        weights={"communication": 1.0}, confidence=0.5, needs_human_review=False,
    )  # fmt: skip


def test_entry_moments_are_the_lines_that_named_the_candidate():
    """Entry moments are the lines where others turned to the candidate by name."""
    moments = entry_moments(SEGMENTS, "A", "Asha Rao", NAMES)
    assert [(m["timestamp"], m["speaker"]) for m in moments] == [
        ("0:04", "Chitra"),
        ("1:15", "Bhavana"),
    ]


def test_a_silent_candidates_debrief_is_about_getting_in():
    """A silent candidate's feedback focuses on how to come into the discussion."""
    moments = entry_moments(SEGMENTS, "A", "Asha Rao", NAMES)
    report = build_candidate_report(
        "A", "Asha Rao", _score(), [], _metrics(0, 0.0), moments=moments
    )
    assert report.weaknesses[0].startswith("You didn't come into the discussion this time")
    # The sentence that names them — the invitation — not the first sixteen words.
    assert report.weaknesses[1].startswith(
        'At 0:04, Chitra turned to you: "Asha, what\'s your take first?"'
    )
    assert "Overall:" not in report.feedback
    assert "landed" not in report.feedback
    assert report.entry_moments == moments


def test_a_quiet_candidate_is_not_told_their_points_landed():
    """A quiet candidate is not told their points landed."""
    report = build_candidate_report("A", "Asha Rao", _score(), [], _metrics(2, 0.05))
    joined = " ".join(report.weaknesses)
    assert "less than an even share" in joined
    assert "landed" not in joined


def test_a_quiet_candidates_synthesis_is_given_the_openings():
    """Rule 14: the narrative is built on real moments, not on what was missing."""
    from services.evaluation.synthesis import build_prompt, openings_for

    openings = openings_for("A", _metrics(0, 0.0), SEGMENTS)
    assert openings == [
        "  0:04 We're here to discuss internships. Asha, what's your take first?",
        "  1:15 asha, do you agree with Arjun on that?",
    ]
    prompt = build_prompt("A", [], _metrics(0, 0.0), "Internships", [], openings)
    assert "OPENINGS (questions others put to the group" in prompt


def test_an_active_candidate_gets_no_openings_block():
    """An active candidate gets no 'openings' section."""
    from services.evaluation.synthesis import openings_for

    assert openings_for("A", _metrics(6, 0.4), SEGMENTS) == []


async def test_a_silent_candidates_narrative_is_kind_and_names_the_first_opening():
    """Told rule 14, the model still opened with "you contributed nothing"; now it
    is written without a model, from the openings."""
    from services.common.contracts import EvaluationOutput
    from services.evaluation.synthesis import synthesise

    output = EvaluationOutput.model_construct(dimension="communication", score=1.0)
    result = await synthesise(
        "A", [output], {"communication": 1.0}, _metrics(0, 0.0), "T", SEGMENTS
    )
    assert result.model == "deterministic"
    assert result.summary.startswith("You stayed quiet in this one")
    assert "The first came at 0:04" in result.summary
    for phrase in ("contributed nothing", "failed", "bottom", "very low"):
        assert phrase not in result.summary.lower()
