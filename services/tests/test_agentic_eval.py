"""Critic hardening, progress tracking and the benchmark helpers."""

from services.common.contracts import EvaluationOutput, EvidenceItem, TranscriptSegment
from services.evaluation.benchmark import agreement, evidence_accuracy
from services.evaluation.progress import progress
from services.evaluation.tools import audit_evidence

SEGS = [
    TranscriptSegment(
        session_id="s", candidate_id="A", start=0, end=20, confidence=0.9,
        text="Remote work saves commuting time and gives employees flexibility.",
    )
]


def _out(summary: str, confidence: float = 0.5, n: int = 1) -> EvaluationOutput:
    return EvaluationOutput(
        candidate_id="A", dimension="content", score=6, confidence=confidence,
        evidence=[EvidenceItem(timestamp="00:05", summary=summary)] * n,
    )


def test_a_real_quote_passes_and_an_invented_one_is_caught():
    assert audit_evidence(_out('said "saves commuting time and gives employees"'), SEGS) == []
    fake = audit_evidence(_out('said "pays no tax at all"'), SEGS)
    assert any("quote not found" in i for i in fake)


def test_confidence_needs_more_than_one_moment():
    assert audit_evidence(_out("relevant point", confidence=0.95, n=1), SEGS)
    assert audit_evidence(_out("relevant point", confidence=0.95, n=2), SEGS) == []


def test_progress_compares_only_shared_dimensions():
    got = progress({"content": 7, "leadership": 5}, [{"content": 5}, {"content": 6}])
    assert got["dimensions"]["content"]["delta"] == 1.5
    assert "leadership" not in got["dimensions"]
    assert progress({"content": 7}, []) == {}


def test_agreement_and_evidence_accuracy():
    got = agreement({"A": {"content": 6, "leadership": 4}}, {"A": {"content": 7, "leadership": 4}})
    assert got["mae"] == 0.5 and got["within_1"] == 1.0
    assert evidence_accuracy([_out("ok"), _out('said "never uttered this phrase"')], SEGS) == 0.5
