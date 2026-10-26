"""Datasets and experiments: save a call, replay it against another model or prompt, compare.

Purpose:     The LangSmith loop. Save good (or corrected) calls as test cases, rerun the
             whole set with a different model or system prompt, and see how closely the
             new answers agree with the expected ones, at what latency and cost.
Why needed:  Changing a prompt or model is a guess until it is run on the same inputs as
             before. This makes that a button instead of a hunch.
Limits:      A replay is one plain JSON call: tools are not re-run, so a case saved from a
             tool-using agent is replayed without its lookups.
"""

import asyncio
import time
import uuid
from collections import defaultdict
from typing import Any

from sqlalchemy import delete, func, select
from sqlalchemy.ext.asyncio import AsyncSession

from services.common.errors import ConflictError, NotFoundError
from services.llm import get_provider_for_model
from services.observability.context import trace_context
from services.observability.models import EvalCase, EvalResult, Span
from services.observability.pricing import cost_usd
from services.observability.service import percentile

MAX_CASES_PER_RUN = 50
CONCURRENCY = 4
# Free text is compared by eye, not by equality: only short values count as comparable.
SHORT_TEXT = 40
NUMBER_TOLERANCE = 1.0  # scores are 0-10, so one point apart still agrees


def match_score(expected: Any, actual: Any) -> float | None:
    """How closely `actual` agrees with `expected`, 0..1, or None if nothing is comparable.

    Booleans, short strings and enums must be equal, numbers within a point, and lists
    and objects are compared element by element. Long free text is skipped.
    """
    if isinstance(expected, bool) or expected is None:
        return float(expected == actual)
    if isinstance(expected, int | float):
        if isinstance(actual, bool) or not isinstance(actual, int | float):
            return 0.0
        return float(abs(expected - actual) <= NUMBER_TOLERANCE)
    if isinstance(expected, str):
        if len(expected) > SHORT_TEXT:
            return None
        return float(isinstance(actual, str) and expected.strip().lower() == actual.strip().lower())
    if isinstance(expected, dict):
        actual = actual if isinstance(actual, dict) else {}
        parts = [match_score(v, actual.get(k)) for k, v in expected.items()]
    elif isinstance(expected, list):
        actual = actual if isinstance(actual, list) else []
        parts = [match_score(e, a) for e, a in zip(expected, actual, strict=False)]
        parts += [0.0] * abs(len(expected) - len(actual))  # a missing or extra item disagrees
    else:
        return None
    parts = [p for p in parts if p is not None]
    return round(sum(parts) / len(parts), 4) if parts else None


async def save_case(
    db: AsyncSession, *, span_id: uuid.UUID, dataset: str, note: str, expected: dict | None
) -> EvalCase:
    """Turn a recorded call into a test case; the recorded output is the default expectation."""
    row = await db.get(Span, span_id)
    if row is None:
        raise NotFoundError("span not found")
    schema = (row.meta or {}).get("schema")
    if row.kind != "llm" or not (row.prompt and row.system_prompt and schema):
        raise ConflictError(
            "this call has no stored prompt and schema (is trace_store_content off?)"
        )
    case = EvalCase(
        dataset=dataset.strip(),
        source_span_id=row.id,
        purpose=row.purpose,
        agent=row.agent,
        model=row.model,
        system_prompt=row.system_prompt,
        prompt=row.prompt,
        schema=schema,
        expected=expected if expected is not None else (row.output or {}),
        note=note,
    )
    db.add(case)
    await db.flush()
    return case


async def delete_case(db: AsyncSession, case_id: uuid.UUID) -> None:
    """Remove a case and its past results."""
    await db.execute(delete(EvalResult).where(EvalResult.case_id == case_id))
    await db.execute(delete(EvalCase).where(EvalCase.id == case_id))


def case_dict(case: EvalCase) -> dict[str, Any]:
    """A case as the API shows it."""
    return {
        "id": str(case.id),
        "dataset": case.dataset,
        "created_at": case.created_at.isoformat(),
        "purpose": case.purpose,
        "agent": case.agent,
        "model": case.model,
        "prompt": case.prompt,
        "system_prompt": case.system_prompt,
        "expected": case.expected,
        "note": case.note,
    }


async def list_datasets(db: AsyncSession) -> list[dict[str, Any]]:
    """Each dataset with its case count and how many runs it has had."""
    cases = (
        await db.execute(select(EvalCase.dataset, func.count()).group_by(EvalCase.dataset))
    ).all()
    runs = dict(
        (
            await db.execute(
                select(EvalResult.dataset, func.count(func.distinct(EvalResult.run_id))).group_by(
                    EvalResult.dataset
                )
            )
        ).all()
    )
    return [{"dataset": d, "cases": n, "runs": runs.get(d, 0)} for d, n in sorted(cases)]


async def list_cases(db: AsyncSession, dataset: str) -> list[dict[str, Any]]:
    """The cases of a dataset, oldest first."""
    rows = await db.execute(
        select(EvalCase).where(EvalCase.dataset == dataset).order_by(EvalCase.created_at)
    )
    return [case_dict(c) for c in rows.scalars()]


async def run_dataset(
    db: AsyncSession, dataset: str, *, model: str, system_prompt: str, label: str
) -> dict[str, Any]:
    """Replay every case once (bounded concurrency), store the results, summarise the run."""
    cases = list(
        (
            await db.execute(
                select(EvalCase)
                .where(EvalCase.dataset == dataset)
                .order_by(EvalCase.created_at)
                .limit(MAX_CASES_PER_RUN)
            )
        ).scalars()
    )
    if not cases:
        raise NotFoundError("dataset has no cases")
    provider = get_provider_for_model(model)
    run_id = str(uuid.uuid4())
    gate = asyncio.Semaphore(CONCURRENCY)

    async def one(case: EvalCase) -> EvalResult:
        async with gate:
            result = EvalResult(
                run_id=run_id, dataset=dataset, label=label, case_id=case.id, model=provider.model
            )
            started = time.perf_counter()
            with trace_context(purpose="experiment", agent=dataset, run=label or run_id[:8]):
                try:
                    answer = await provider.complete_json(
                        system=system_prompt or case.system_prompt,
                        prompt=case.prompt,
                        schema=case.schema,
                    )
                except Exception as exc:  # noqa: BLE001 - one bad case must not stop the run
                    result.error = f"{type(exc).__name__}: {exc}"[:2000]
                    return result
            result.latency_ms = (time.perf_counter() - started) * 1000
            result.output = answer.data
            result.input_tokens = answer.input_tokens
            result.output_tokens = answer.output_tokens
            result.model = answer.model or provider.model
            result.cost_usd = cost_usd(result.model, answer.input_tokens, answer.output_tokens)
            result.match = match_score(case.expected, answer.data)
            return result

    results = await asyncio.gather(*(one(c) for c in cases))
    db.add_all(results)
    await db.flush()
    return summarise(list(results)) | {"run_id": run_id}


def summarise(rows: list[EvalResult]) -> dict[str, Any]:
    """Headline numbers for one run."""
    scored = [r.match for r in rows if r.match is not None]
    latencies = [r.latency_ms for r in rows if r.error is None]
    first = rows[0]
    return {
        "label": first.label,
        "model": first.model,
        "created_at": min(r.created_at for r in rows if r.created_at).isoformat()
        if all(r.created_at for r in rows)
        else None,
        "cases": len(rows),
        "errors": sum(r.error is not None for r in rows),
        "avg_match": round(sum(scored) / len(scored), 4) if scored else None,
        "p50_ms": percentile(latencies, 50),
        "p95_ms": percentile(latencies, 95),
        "input_tokens": sum(r.input_tokens for r in rows),
        "output_tokens": sum(r.output_tokens for r in rows),
        "cost_usd": round(sum(r.cost_usd for r in rows), 6),
    }


async def list_runs(db: AsyncSession, dataset: str) -> list[dict[str, Any]]:
    """Past runs of a dataset, newest first, ready to compare side by side."""
    rows = (
        await db.execute(
            select(EvalResult).where(EvalResult.dataset == dataset).order_by(EvalResult.created_at)
        )
    ).scalars()
    runs: dict[str, list[EvalResult]] = defaultdict(list)
    for row in rows:
        runs[row.run_id].append(row)
    out = [{"run_id": run_id, **summarise(group)} for run_id, group in runs.items()]
    return sorted(out, key=lambda r: r["created_at"] or "", reverse=True)


async def run_results(db: AsyncSession, run_id: str) -> list[dict[str, Any]]:
    """Every case's answer in a run, beside what was expected."""
    rows = (
        await db.execute(
            select(EvalResult, EvalCase)
            .join(EvalCase, EvalCase.id == EvalResult.case_id)
            .where(EvalResult.run_id == run_id)
            .order_by(EvalCase.created_at)
        )
    ).all()
    return [
        {
            "case_id": str(r.case_id),
            "prompt": c.prompt[:300],
            "expected": c.expected,
            "output": r.output,
            "match": r.match,
            "latency_ms": r.latency_ms,
            "cost_usd": r.cost_usd,
            "error": r.error,
        }
        for r, c in rows
    ]
