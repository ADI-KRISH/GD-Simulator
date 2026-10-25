"""Measure the agentic pipeline against a single-LLM baseline and human scores (design §12).

Purpose:     The research question is whether planning + tools + critic beat one
             LLM call. This gives the three numbers needed to answer it: agreement
             with human raters, evidence accuracy, and a baseline to compare to.
Contains:
  - `agreement`          MAE / Pearson / within-1 between predicted and human scores.
  - `evidence_accuracy`  share of cited moments that pass the critic's audit.
  - `single_llm_baseline` one prompt, whole transcript, one score per candidate and
                         dimension; no tools, no critic.
Used by:     tests/test_benchmark.py; run it over human-rated sessions offline.
"""

from statistics import correlation

from services.common.contracts import DIMENSIONS, EvaluationOutput, TranscriptSegment
from services.evaluation.tools import audit_evidence
from services.llm import get_llm_provider

Scores = dict[str, dict[str, float]]  # candidate -> dimension -> 0-10


def agreement(predicted: Scores, human: Scores) -> dict[str, float]:
    """Compare two score sets on the (candidate, dimension) pairs both contain."""
    pairs = [
        (predicted[c][d], h)
        for c, dims in human.items()
        for d, h in dims.items()
        if d in predicted.get(c, {})
    ]
    if not pairs:
        return {"n": 0}
    errors = [abs(p - h) for p, h in pairs]
    result = {
        "n": len(pairs),
        "mae": round(sum(errors) / len(errors), 3),
        "within_1": round(sum(e <= 1 for e in errors) / len(errors), 3),
    }
    # Pearson is undefined when either side is constant or there are fewer than 2 pairs.
    if len(pairs) > 1 and len({p for p, _ in pairs}) > 1 and len({h for _, h in pairs}) > 1:
        result["pearson"] = round(correlation(*zip(*pairs, strict=True)), 3)
    return result


def evidence_accuracy(
    outputs: list[EvaluationOutput], segments: list[TranscriptSegment]
) -> float:
    """Fraction of verdicts whose cited evidence passes the audit with no problem."""
    if not outputs:
        return 0.0
    return round(sum(not audit_evidence(o, segments) for o in outputs) / len(outputs), 3)


async def single_llm_baseline(
    topic: str, candidates: list[str], segments: list[TranscriptSegment]
) -> Scores:
    """The comparison arm: one model call scores everyone on every dimension."""
    transcript = "\n".join(
        f"[{int(s.start) // 60:02d}:{int(s.start) % 60:02d}] {s.candidate_id}: {s.text}"
        for s in sorted(segments, key=lambda s: s.start)
    )
    scores = {"type": "number", "minimum": 0, "maximum": 10}
    schema = {
        "type": "object",
        "required": candidates,
        "additionalProperties": False,
        "properties": {
            c: {
                "type": "object",
                "required": list(DIMENSIONS),
                "additionalProperties": False,
                "properties": {d: scores for d in DIMENSIONS},
            }
            for c in candidates
        },
    }
    result = await get_llm_provider().complete_json(
        system="You score group-discussion candidates 0-10 on each dimension.",
        prompt=f"Topic: {topic}\n\nTranscript:\n{transcript}\n\nScore every candidate.",
        schema=schema,
    )
    return {c: {d: float(v) for d, v in dims.items()} for c, dims in result.data.items()}
