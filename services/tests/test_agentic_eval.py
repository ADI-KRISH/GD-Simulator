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


async def test_purge_audio_deletes_files_and_rows(gd_session, session_factory, tmp_path):
    import uuid

    from services.common.storage import FilesystemStore, set_object_store
    from services.evaluation.service import purge_audio
    from services.speech import repository as speech_repo
    from services.speech.models import AudioChunk

    store = FilesystemStore(tmp_path)
    set_object_store(store)
    sid = uuid.UUID(gd_session["id"])
    try:
        async with session_factory() as db:
            await store.put("a/1.webm", b"x")
            await speech_repo.add_audio_chunk(
                db, AudioChunk(session_id=sid, candidate_label="A", sequence=0, start=0, end=5,
                               storage_key="a/1.webm")
            )
            await purge_audio(db, sid)
            assert not await store.exists("a/1.webm")
            assert await speech_repo.list_audio_chunks(db, sid) == []
    finally:
        set_object_store(None)


def _tool(name, segs):
    from services.evaluation.tools import build_evidence_tools

    tools = build_evidence_tools(segs, None, [])  # verify_segment never reads metrics
    return next(t for t in tools if t.name == name).handler


def _seg(who, start, end, conf=0.9):
    return TranscriptSegment(
        session_id="s", candidate_id=who, start=start, end=end, confidence=conf, text="x"
    )


def test_verify_segment_confirms_flags_overlap_and_low_confidence():
    verify = _tool("verify_segment", [_seg("A", 0, 10), _seg("B", 5, 8), _seg("A", 100, 110, 0.4)])
    assert verify("A", 1)["verdict"] == "uncertain"  # B overlaps A
    assert verify("A", 105)["verdict"] == "uncertain"  # low attribution confidence
    assert verify("B", 50)["verdict"] == "not_speaking"
    assert _tool("verify_segment", [_seg("A", 0, 10)])("A", 5)["verdict"] == "confirmed"


def test_evidence_resting_only_on_uncertain_attribution_is_flagged():
    segs = [_seg("A", 0, 20, 0.3)]
    assert any("uncertain" in i for i in audit_evidence(_out("a point"), segs))
