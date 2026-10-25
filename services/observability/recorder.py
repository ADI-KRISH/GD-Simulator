"""Record a span without ever slowing or breaking the call being traced.

Purpose:     `record` builds the row (cost included) and writes it in a background
             task; `span` times a block (speech, a latency measurement) and records it.
Why needed:  The tracer sits on the hot path of a live conversation. A slow or
             failing database write must cost that conversation nothing, so writes
             are fire-and-forget and every failure is swallowed after a log line.
Contains:    `set_sink` (tests capture spans instead of writing them), `record`, `span`.
"""

import asyncio
import logging
import time
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from datetime import UTC, datetime
from typing import Any

from services.common.config import settings
from services.common.db import get_session_factory
from services.observability.context import current
from services.observability.models import Span
from services.observability.pricing import cost_usd, price_of

logger = logging.getLogger(__name__)

_sink: Callable[[dict], None] | None = None
_pending: set[asyncio.Task] = set()  # keeps background writes from being garbage-collected
_COLUMNS = {c.name for c in Span.__table__.columns}

TEXT_LIMIT = 60_000  # characters of prompt/output kept per span


def set_sink(sink: Callable[[dict], None] | None) -> None:
    """Send finished spans to `sink` instead of the database (tests)."""
    global _sink
    _sink = sink


def _clip(text: str | None) -> str | None:
    """Respect the privacy switch and the size cap."""
    if text is None or not settings.trace_store_content:
        return None
    return text[:TEXT_LIMIT]


def record(**fields: Any) -> None:
    """Record one span. `fields` are the Span columns; the caller's tags are added."""
    fields = {**current(), **fields}
    if _sink is not None:
        _sink(fields)
        return
    if not settings.trace_enabled:
        return
    model = fields.get("model", "")
    row = Span(
        **{k: v for k, v in fields.items() if k in _COLUMNS},
        cost_usd=cost_usd(model, fields.get("input_tokens", 0), fields.get("output_tokens", 0)),
        priced=fields.get("kind") != "llm" or price_of(model) is not None,
    )
    row.started_at = fields.get("started_at") or datetime.now(UTC)
    row.system_prompt = _clip(row.system_prompt)
    row.prompt = _clip(row.prompt)
    if not settings.trace_store_content:
        row.output = None
    try:
        task = asyncio.get_running_loop().create_task(_write(row))
    except RuntimeError:
        return  # no running loop (sync caller): nothing to write from
    _pending.add(task)
    task.add_done_callback(_pending.discard)


async def _write(row: Span) -> None:
    """Persist one span; a failure is logged and dropped."""
    try:
        async with get_session_factory()() as db:
            db.add(row)
            await db.commit()
    except Exception as exc:  # noqa: BLE001 - tracing must never raise into the app
        logger.warning("span not saved", extra={"error": str(exc)})


@asynccontextmanager
async def span(kind: str, name: str, **fields: Any) -> AsyncIterator[dict]:
    """Time the block and record it. The block may add fields to the yielded dict."""
    data: dict = {"kind": kind, "name": name, "started_at": datetime.now(UTC), **fields}
    t0 = time.perf_counter()
    try:
        yield data
    except Exception as exc:
        data["status"] = "error"
        data["error"] = f"{type(exc).__name__}: {exc}"[:2000]
        raise
    finally:
        data["latency_ms"] = (time.perf_counter() - t0) * 1000
        record(**data)
