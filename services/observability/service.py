"""Read side of the tracer: the numbers and rows the dashboard shows.

Purpose:     Turns raw spans into totals, breakdowns, a time series, per-session
             traces, a filterable call list, and quality signals taken from the
             evaluation tables.
Why needed:  The dashboard needs "what is slow, what is costly, what is failing"
             answered in one call. Aggregation is done in Python over the window's
             rows: portable across SQLite and Postgres, and plenty for a local system.
"""

import uuid
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from typing import Any

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from services.evaluation.models import AgentOutput, CandidateScore
from services.observability.models import Span

ROW_CAP = 50_000  # most spans one summary reads
_LIGHT = (
    Span.started_at, Span.kind, Span.purpose, Span.name, Span.model, Span.agent,
    Span.session_id, Span.latency_ms, Span.ttfb_ms, Span.input_tokens, Span.output_tokens,
    Span.cost_usd, Span.priced, Span.status,
)  # fmt: skip


def percentile(values: list[float], pct: float) -> float:
    """Nearest-rank percentile; 0.0 for an empty list."""
    if not values:
        return 0.0
    ordered = sorted(values)
    return round(ordered[min(len(ordered) - 1, int(len(ordered) * pct / 100))], 1)


def _stats(rows: list) -> dict[str, Any]:
    """Calls, errors, tokens, cost and latency percentiles for a group of span rows."""
    latencies = [r.latency_ms for r in rows]
    return {
        "calls": len(rows),
        "errors": sum(r.status == "error" for r in rows),
        "input_tokens": sum(r.input_tokens for r in rows),
        "output_tokens": sum(r.output_tokens for r in rows),
        "cost_usd": round(sum(r.cost_usd for r in rows), 6),
        "p50_ms": percentile(latencies, 50),
        "p95_ms": percentile(latencies, 95),
        "p99_ms": percentile(latencies, 99),
        "mean_ms": round(sum(latencies) / len(latencies), 1) if latencies else 0.0,
    }


def _group(rows: list, key) -> list[dict[str, Any]]:
    """Per-key stats, most expensive first."""
    groups: dict[str, list] = defaultdict(list)
    for row in rows:
        groups[key(row) or "-"].append(row)
    out = [{"key": k, **_stats(v)} for k, v in groups.items()]
    return sorted(out, key=lambda g: (-g["cost_usd"], -g["calls"]))


def _bucket(hours: int) -> timedelta:
    """Chart resolution for the window."""
    return (
        timedelta(minutes=5)
        if hours <= 6
        else timedelta(hours=1)
        if hours <= 72
        else timedelta(days=1)
    )


def _as_utc(moment: datetime) -> datetime:
    """SQLite hands datetimes back naive; they were stored as UTC."""
    return moment if moment.tzinfo else moment.replace(tzinfo=UTC)


async def _window(db: AsyncSession, hours: int, session_id: str | None = None) -> list:
    """Light span rows in the window, newest first."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    query = select(*_LIGHT).where(Span.started_at >= since).order_by(Span.started_at.desc())
    if session_id:
        query = query.where(Span.session_id == session_id)
    return list((await db.execute(query.limit(ROW_CAP))).all())


async def summary(db: AsyncSession, hours: int, session_id: str | None = None) -> dict[str, Any]:
    """Everything the overview tab shows."""
    rows = await _window(db, hours, session_id)
    llm = [r for r in rows if r.kind == "llm"]
    speech = {kind: [r for r in rows if r.kind == kind] for kind in ("stt", "tts")}
    replies = [r.latency_ms for r in rows if r.kind == "latency" and r.name == "reply_latency"]
    ttfb = [r.ttfb_ms for r in speech["tts"] if r.ttfb_ms is not None]

    step = _bucket(hours)
    buckets: dict[datetime, list] = defaultdict(list)
    epoch = datetime(1970, 1, 1, tzinfo=UTC)
    for row in llm:
        started = _as_utc(row.started_at)
        buckets[epoch + ((started - epoch) // step) * step].append(row)
    series = []
    for t, group in sorted(buckets.items()):
        stats = _stats(group)
        series.append(
            {
                "t": t.isoformat(),
                "calls": stats["calls"],
                "errors": stats["errors"],
                "cost_usd": stats["cost_usd"],
                "p95_ms": stats["p95_ms"],
                "tokens": stats["input_tokens"] + stats["output_tokens"],
            }
        )

    return {
        "window_hours": hours,
        "totals": {
            **_stats(llm),
            "unpriced_calls": sum(not r.priced for r in llm),
            "error_rate": round(sum(r.status == "error" for r in llm) / len(llm), 4)
            if llm
            else 0.0,
        },
        "by_purpose": _group(llm, lambda r: r.purpose),
        "by_model": _group(llm, lambda r: r.model),
        "by_agent": _group(llm, lambda r: r.agent or r.name),
        "speech": {
            "stt": _stats(speech["stt"]),
            "tts": {
                **_stats(speech["tts"]),
                "ttfb_p50_ms": percentile(ttfb, 50),
                "ttfb_p95_ms": percentile(ttfb, 95),
            },
        },  # fmt: skip
        "reply_latency": {
            "count": len(replies),
            "p50_ms": percentile(replies, 50),
            "p95_ms": percentile(replies, 95),
            "max_ms": round(max(replies), 1) if replies else 0.0,
        },  # fmt: skip
        "series": series,
        "truncated": len(rows) >= ROW_CAP,
    }


async def traces(db: AsyncSession, hours: int) -> list[dict[str, Any]]:
    """One roll-up per session: what it cost and how long its calls took."""
    groups: dict[str, list] = defaultdict(list)
    for row in await _window(db, hours):
        if row.session_id:
            groups[row.session_id].append(row)
    out = []
    for session_id, rows in groups.items():
        llm = [r for r in rows if r.kind == "llm"]
        replies = [r.latency_ms for r in rows if r.kind == "latency" and r.name == "reply_latency"]
        out.append({
            "session_id": session_id,
            **_stats(llm),
            "started_at": min(_as_utc(r.started_at) for r in rows).isoformat(),
            "last_at": max(_as_utc(r.started_at) for r in rows).isoformat(),
            "purposes": sorted({r.purpose for r in llm}),
            "reply_p50_ms": percentile(replies, 50),
            "reply_p95_ms": percentile(replies, 95),
        })  # fmt: skip
    return sorted(out, key=lambda t: t["last_at"], reverse=True)


async def trace_tree(db: AsyncSession, session_id: str) -> list[dict[str, Any]]:
    """Every span of one session with its parent link, oldest first (the waterfall's input)."""
    rows = await db.execute(
        select(Span.id, Span.parent_id, *_LIGHT, Span.error)
        .where(Span.session_id == session_id)
        .order_by(Span.started_at)
        .limit(3000)
    )
    return [
        {
            **r._asdict(),
            "id": str(r.id),
            "parent_id": str(r.parent_id) if r.parent_id else None,
            "started_at": _as_utc(r.started_at).isoformat(),
        }
        for r in rows
    ]


async def alerts(db: AsyncSession) -> list[dict[str, str]]:
    """What is wrong in the last hour, judged against the thresholds in settings."""
    from services.common.config import settings

    data = await summary(db, 1)
    totals, reply = data["totals"], data["reply_latency"]
    found: list[dict[str, str]] = []
    if totals["calls"] >= 5 and totals["error_rate"] > settings.alert_error_rate:
        found.append({
            "level": "error", "title": "High error rate",
            "detail": f"{totals['error_rate'] * 100:.0f}% of {totals['calls']} model calls failed "
                      f"in the last hour (limit {settings.alert_error_rate * 100:.0f}%).",
        })  # fmt: skip
    if totals["calls"] >= 5 and totals["p95_ms"] > settings.alert_p95_ms:
        found.append({
            "level": "warn", "title": "Slow model calls",
            "detail": f"p95 latency {totals['p95_ms'] / 1000:.1f} s (limit "
                      f"{settings.alert_p95_ms / 1000:.0f} s).",
        })  # fmt: skip
    if reply["count"] >= 3 and reply["p95_ms"] > settings.alert_reply_p95_ms:
        found.append({
            "level": "warn", "title": "Slow replies in practice",
            "detail": f"Students waited {reply['p95_ms'] / 1000:.1f} s at p95 for a persona to "
                      f"answer (limit {settings.alert_reply_p95_ms / 1000:.0f} s).",
        })  # fmt: skip
    if totals["cost_usd"] > settings.alert_cost_usd_per_hour:
        found.append({
            "level": "warn", "title": "Spend above budget",
            "detail": f"${totals['cost_usd']:.2f} in the last hour (limit "
                      f"${settings.alert_cost_usd_per_hour:.2f}).",
        })  # fmt: skip
    if totals["unpriced_calls"]:
        found.append({
            "level": "info", "title": "Unpriced model",
            "detail": f"{totals['unpriced_calls']} calls used a model with no price in "
                      "observability/pricing.py, so their cost shows as $0.",
        })  # fmt: skip
    return found


async def list_spans(
    db: AsyncSession,
    *,
    hours: int,
    kind: str | None,
    purpose: str | None,
    status: str | None,
    agent: str | None,
    model: str | None,
    session_id: str | None,
    q: str | None,
    limit: int,
    offset: int,
) -> list[dict[str, Any]]:
    """Filtered call list, newest first, with a short prompt preview."""
    query = select(
        Span.id, *_LIGHT, Span.error, func.substr(Span.prompt, 1, 160).label("preview")
    ).where(Span.started_at >= datetime.now(UTC) - timedelta(hours=hours))
    for column, value in (
        (Span.kind, kind), (Span.purpose, purpose), (Span.status, status),
        (Span.agent, agent), (Span.model, model), (Span.session_id, session_id),
    ):  # fmt: skip
        if value:
            query = query.where(column == value)
    if q:
        like = f"%{q}%"
        query = query.where(
            Span.prompt.ilike(like) | Span.name.ilike(like) | Span.error.ilike(like)
        )
    rows = (
        await db.execute(query.order_by(Span.started_at.desc()).limit(limit).offset(offset))
    ).all()
    return [
        {**r._asdict(), "id": str(r.id), "started_at": _as_utc(r.started_at).isoformat()}
        for r in rows
    ]


async def get_span(db: AsyncSession, span_id: uuid.UUID) -> dict[str, Any] | None:
    """One span in full: prompts, output, tool calls."""
    row = await db.get(Span, span_id)
    if row is None:
        return None
    data = {c.name: getattr(row, c.name) for c in Span.__table__.columns}
    return {**data, "id": str(row.id), "started_at": _as_utc(row.started_at).isoformat()}


async def quality(db: AsyncSession, hours: int) -> dict[str, Any]:
    """How good the evaluators' work looks, from what they stored (not from the model)."""
    since = datetime.now(UTC) - timedelta(hours=hours)
    outputs = list(
        (await db.execute(select(AgentOutput).where(AgentOutput.created_at >= since))).scalars()
    )
    scores = list(
        (
            await db.execute(select(CandidateScore).where(CandidateScore.created_at >= since))
        ).scalars()
    )
    by_agent: dict[str, list[AgentOutput]] = defaultdict(list)
    for output in outputs:
        by_agent[output.agent_name].append(output)

    def mean(values: list[float]) -> float:
        return round(sum(values) / len(values), 3) if values else 0.0

    return {
        "window_hours": hours,
        "candidates_scored": len(scores),
        "human_review_rate": round(sum(s.needs_human_review for s in scores) / len(scores), 3)
        if scores
        else 0.0,
        "agents": sorted(
            (
                {
                    "agent": agent,
                    "verdicts": len(rows),
                    "avg_score": mean([r.score for r in rows]),
                    "avg_confidence": mean([r.confidence for r in rows]),
                    # A verdict the critic had something to say about.
                    "critic_issue_rate": mean(
                        [1.0 if (r.provenance or {}).get("critic") else 0.0 for r in rows]
                    ),
                    "avg_tool_calls": mean(
                        [float(len((r.provenance or {}).get("tool_calls") or [])) for r in rows]
                    ),
                    "avg_evidence": mean([float(len(r.evidence or [])) for r in rows]),
                }
                for agent, rows in by_agent.items()
            ),
            key=lambda a: a["agent"],
        ),  # fmt: skip
    }
