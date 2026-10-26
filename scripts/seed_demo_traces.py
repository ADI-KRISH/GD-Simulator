"""Fill the database with SYNTHETIC traces so the Observability dashboard has something to show.

Not real usage: every number is generated. Run it against a throwaway database:

    DATABASE_URL=sqlite+aiosqlite:///./gd_demo.db alembic upgrade head
    DATABASE_URL=sqlite+aiosqlite:///./gd_demo.db python scripts/seed_demo_traces.py
"""

import asyncio
import random
import uuid
from datetime import UTC, datetime, timedelta

from services.common.db import SessionFactory
from services.observability.models import EvalCase, Span
from services.observability.pricing import cost_usd

rng = random.Random(7)
NOW = datetime.now(UTC)
DIMENSIONS = ["content", "communication", "leadership", "collaboration", "critical_thinking", "behavior"]
PERSONAS = ["Arjun", "Bhavana", "Chitra", "Dev"]
SCHEMA = {"type": "object", "properties": {"speak": {}, "is_conclusion": {}, "text": {}}}
spans: list[Span] = []


def add(started: datetime, kind: str, name: str, ms: float, **kw) -> Span:
    """Create one span (cost filled in from tokens) and remember it."""
    model = kw.get("model", "")
    tokens = (kw.get("input_tokens", 0), kw.get("output_tokens", 0))
    kw.setdefault("priced", True)
    kw.setdefault("id", uuid.uuid4())  # children need the parent id before it is saved
    row = Span(
        started_at=started, kind=kind, name=name, latency_ms=ms,
        cost_usd=cost_usd(model, *tokens), **kw,
    )  # fmt: skip
    spans.append(row)
    return row


def practice_session(start: datetime, model: str, slow: bool) -> None:
    """A solo practice session: the student speaks, a persona answers, repeatedly."""
    sid = str(uuid.uuid4())
    t = start
    for turn in range(rng.randint(6, 11)):
        persona = rng.choice(PERSONAS)
        t += timedelta(seconds=rng.randint(12, 30))
        stt = add(t, "stt", "openai", rng.gauss(650, 120), purpose="practice", session_id=sid,
                  provider="openai", meta={"audio_bytes": rng.randint(30_000, 90_000)})  # fmt: skip
        chain = add(t + timedelta(seconds=1), "chain", f"persona turn: {persona}", 0.0,
                    purpose="persona", session_id=sid, agent=persona)  # fmt: skip
        llm_ms = rng.gauss(2600 if slow else 1100, 400)
        failed = rng.random() < (0.12 if slow else 0.03)
        add(t + timedelta(seconds=1.2), "llm", persona, llm_ms, purpose="persona", session_id=sid,
            agent=persona, parent_id=chain.id, provider="openai", model=model,
            input_tokens=rng.randint(900, 1300), output_tokens=rng.randint(40, 120),
            status="error" if failed else "ok",
            error="LLMError: json_validate_failed" if failed else None,
            system_prompt=f"You are {persona}, taking part in a live spoken group discussion...",
            prompt="Discussion so far:\n...\n\nWhat do you say next?",
            output=None if failed else {"speak": True, "is_conclusion": False,
                                        "text": "That is fair, but who pays for the transition?"},
            meta={"max_tokens": 400, "schema": SCHEMA})  # fmt: skip
        if not failed:
            ttfb = rng.gauss(420, 90)
            add(t + timedelta(seconds=1.2 + llm_ms / 1000), "tts", persona, ttfb + 600,
                purpose="persona", session_id=sid, parent_id=chain.id, provider="edge",
                ttfb_ms=ttfb, meta={"audio_bytes": rng.randint(20_000, 60_000)})  # fmt: skip
            add(t + timedelta(seconds=2), "latency", "reply_latency",
                stt.latency_ms + llm_ms + ttfb + 600 + 250, purpose="persona", session_id=sid,
                parent_id=chain.id, meta={"persona": persona, "cue": "reply"})  # fmt: skip
        chain.latency_ms = llm_ms + 4500


def evaluation(start: datetime, model: str) -> None:
    """One evaluation run: six evaluator agents (one revised), synthesis, group report."""
    sid = str(uuid.uuid4())
    run = "eval v1 a1"
    root = add(start, "chain", f"evaluation {run}", 0.0, purpose="evaluation", session_id=sid, run=run)
    t = start
    for dim in DIMENSIONS:
        agent = dim.replace("_", " ")
        for attempt in range(2 if dim == "leadership" else 1):
            chain = add(t, "chain", f"agent: {agent}", 0.0, purpose="evaluation", agent=agent,
                        dimension=dim, session_id=sid, run=run, parent_id=root.id)  # fmt: skip
            ms = rng.gauss(5200, 900)
            out = {"candidate_id": "A", "dimension": dim, "score": round(rng.uniform(4, 9), 1),
                   "confidence": round(rng.uniform(0.55, 0.95), 2), "evidence": []}  # fmt: skip
            add(t, "llm", agent, ms, purpose="evaluation", agent=agent, dimension=dim,
                session_id=sid, run=run, parent_id=chain.id, provider="openai", model=model,
                input_tokens=rng.randint(3000, 5200), output_tokens=rng.randint(350, 700),
                system_prompt="You are a specialist evaluator in a group discussion...",
                prompt="=== SHARED DISCUSSION CONTEXT ===\n...", output=out,
                tool_calls=[{"name": "find_quotes", "arguments": {"keyword": "regulate"},
                             "result": "[{'speaker': 'A', 'at': '01:12'}]"}],
                meta={"max_tokens": None, "schema": SCHEMA})  # fmt: skip
            chain.latency_ms = ms + 150
            t += timedelta(seconds=ms / 1000)
    add(t, "llm", "synthesis", 4300, purpose="synthesis", agent="synthesis", session_id=sid,
        run=run, parent_id=root.id, provider="openai", model=model, input_tokens=2800,
        output_tokens=520, prompt="Candidate A ...", output={"summary": "..."})  # fmt: skip
    root.latency_ms = (t - start).total_seconds() * 1000 + 4300


def main_rows() -> None:
    """The whole synthetic day."""
    for hour in range(23, 0, -1):
        base = NOW - timedelta(hours=hour, minutes=rng.randint(0, 40))
        for _ in range(rng.randint(1, 3)):
            practice_session(base + timedelta(minutes=rng.randint(0, 15)), "openai/gpt-oss-120b", False)
    # A degraded stretch in the last hour: a slower, flakier model.
    for k in range(3):
        practice_session(NOW - timedelta(minutes=50 - k * 15), "openai/gpt-oss-20b", True)
    for k in range(8):  # a burst of rate-limit errors in the last minutes, so an alert shows
        add(NOW - timedelta(minutes=2 + k), "llm", "Dev", 900, purpose="persona", agent="Dev",
            session_id=str(uuid.uuid4()), provider="openai", model="openai/gpt-oss-20b",
            input_tokens=1000, status="error", error="LLMError: rate limited (429)")  # fmt: skip
    for hour in (20, 11, 3):
        evaluation(NOW - timedelta(hours=hour), "openai/gpt-oss-120b")
    for i in range(14):  # observer updates, a few on a model with no listed price
        sid = str(uuid.uuid4())
        add(NOW - timedelta(hours=rng.uniform(0.1, 20)), "llm", "observer", rng.gauss(1900, 300),
            purpose="observer", agent="observer", session_id=sid, provider="openai",
            model="mystery-model-1" if i % 7 == 0 else "openai/gpt-oss-20b",
            priced=i % 7 != 0, input_tokens=rng.randint(700, 1500), output_tokens=rng.randint(120, 300))  # fmt: skip


async def main() -> None:
    """Write the spans and one dataset case."""
    main_rows()
    async with SessionFactory() as db:
        db.add_all(spans)
        source = next(s for s in spans if s.kind == "llm" and s.purpose == "persona" and s.output)
        db.add(
            EvalCase(
                dataset="persona-lines", source_span_id=source.id, purpose="persona",
                agent=source.agent, model=source.model, system_prompt=source.system_prompt or "",
                prompt=source.prompt or "", schema=SCHEMA, expected=source.output or {},
                note="demo case",
            )  # fmt: skip
        )
        await db.commit()
    print(f"seeded {len(spans)} synthetic spans")


if __name__ == "__main__":
    asyncio.run(main())
