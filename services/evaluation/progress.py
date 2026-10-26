"""Progress across sessions: this report's scores against the candidate's own history.

Purpose:     Turns a candidate's earlier dimension scores into per-dimension deltas
             for the report ("communication +1.2 vs your last 3 sessions").
Why needed:  Design §9 asks for longitudinal tracking, with the rule that past
             scores inform coaching and never predetermine a new score. So this
             runs after aggregation and is shown beside the report; no evaluator
             ever sees it.
Used by:     evaluation/service.py (attached to each candidate report).
"""

RECENT = 3  # how many earlier sessions the comparison averages over


def progress(current: dict[str, float], previous: list[dict[str, float]]) -> dict:
    """Delta per dimension against the mean of the last `RECENT` sessions.

    `previous` is newest first. Only dimensions present in both are compared, so a
    session scored under a different rubric is not read as a drop.
    """
    recent = previous[:RECENT]
    deltas = {}
    for dimension, now in current.items():
        before = [p[dimension] for p in recent if dimension in p]
        if before:
            mean = sum(before) / len(before)
            deltas[dimension] = {
                "now": round(now, 2),
                "before": round(mean, 2),
                "delta": round(now - mean, 2),
            }
    return {"sessions_compared": len(recent), "dimensions": deltas} if deltas else {}
