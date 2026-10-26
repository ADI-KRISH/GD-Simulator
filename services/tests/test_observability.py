"""The tracer: what a traced call records, what the dashboard reads back, who may see it."""

import pytest

from services.llm.base import LLMError
from services.llm.stub_provider import StubLLM
from services.observability import recorder, service
from services.observability.context import trace_context
from services.observability.pricing import cost_usd, price_of
from services.observability.tracing import TracingProvider

SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}, "required": ["ok"]}


@pytest.fixture
def spans():
    """Capture spans instead of writing them."""
    got: list[dict] = []
    recorder.set_sink(got.append)
    yield got
    recorder.set_sink(None)


async def test_a_call_is_recorded_with_its_tags_tokens_and_timing(spans):
    provider = TracingProvider(StubLLM())
    with trace_context(purpose="evaluation", agent="content", session_id="s1", candidate="B"):
        result = await provider.complete_json(system="sys", prompt="hello", schema=SCHEMA)
    [span] = spans
    assert span["kind"] == "llm" and span["status"] if "status" in span else True
    assert (span["purpose"], span["agent"], span["session_id"], span["candidate"]) == (
        "evaluation",
        "content",
        "s1",
        "B",
    )
    assert span["prompt"] == "hello" and span["system_prompt"] == "sys"
    assert span["output"] == result.data
    assert span["input_tokens"] == result.input_tokens and span["latency_ms"] >= 0


async def test_a_failed_call_is_recorded_and_still_raises(spans):
    provider = TracingProvider(StubLLM(fail=True))
    with pytest.raises(LLMError):
        await provider.complete_json(system="s", prompt="p", schema=SCHEMA)
    assert spans[0]["status"] == "error" and "LLMError" in spans[0]["error"]


def test_provider_identity_passes_through():
    wrapped = TracingProvider(StubLLM())
    assert (wrapped.name, wrapped.model) == ("stub", "stub-deterministic")


def test_pricing_matches_by_prefix_and_flags_unknown_models():
    assert price_of("openai/gpt-oss-120b") == (0.15, 0.75)
    assert price_of("claude-sonnet-4-5-20250929") == (3.0, 15.0)
    assert price_of("gpt-4o-mini-2024") == (0.15, 0.60)  # longest prefix wins over gpt-4o
    assert price_of("stub-deterministic") is None
    assert cost_usd("claude-sonnet-4-5", 1_000_000, 1_000_000) == 18.0
    assert cost_usd("mystery", 10, 10) == 0.0


async def test_spans_are_stored_and_summarised(session_factory):
    from datetime import UTC, datetime

    from services.observability.models import Span

    now = datetime.now(UTC)
    async with session_factory() as db:
        for i, (status, ms) in enumerate([("ok", 100), ("ok", 300), ("error", 900)]):
            db.add(Span(started_at=now, kind="llm", purpose="persona", name="Arjun", model="m",
                        session_id="s1", latency_ms=ms, input_tokens=10, output_tokens=5,
                        cost_usd=0.01, status=status, prompt=f"p{i}"))  # fmt: skip
        db.add(Span(started_at=now, kind="latency", purpose="persona", name="reply_latency",
                    session_id="s1", latency_ms=1200))  # fmt: skip
        await db.commit()
    async with session_factory() as db:
        out = await service.summary(db, 24)
        assert out["totals"]["calls"] == 3 and out["totals"]["errors"] == 1
        assert out["totals"]["input_tokens"] == 30
        assert out["totals"]["cost_usd"] == pytest.approx(0.03)
        assert out["by_purpose"][0]["key"] == "persona"
        assert out["reply_latency"]["p50_ms"] == 1200
        assert [t["session_id"] for t in await service.traces(db, 24)] == ["s1"]
        listed = await service.list_spans(db, hours=24, kind="llm", purpose=None, status="error",
                                          agent=None, model=None, session_id=None, q=None,
                                          limit=10, offset=0)  # fmt: skip
        assert len(listed) == 1 and listed[0]["preview"] == "p2"
        full = await service.get_span(db, __import__("uuid").UUID(listed[0]["id"]))
        assert full["prompt"] == "p2"


def test_percentile():
    assert service.percentile([], 95) == 0.0
    assert service.percentile([1, 2, 3, 4, 5, 6, 7, 8, 9, 10], 50) == 6
    assert service.percentile([5], 99) == 5


async def test_only_hosts_and_admins_may_read_traces(client, auth_header, host_auth):
    assert (await client.get("/api/v1/observability/summary")).status_code == 401
    denied = await client.get("/api/v1/observability/summary", headers=auth_header)
    assert denied.status_code == 403
    ok = await client.get("/api/v1/observability/summary", headers=host_auth)
    assert ok.status_code == 200 and ok.json()["totals"]["calls"] == 0


# ---- trace tree, alerts, datasets ------------------------------------------------


async def test_calls_inside_a_chain_point_at_it(spans):
    from services.observability.recorder import chain

    provider = TracingProvider(StubLLM())
    async with chain("outer", purpose="evaluation", session_id="s9"):
        async with chain("inner"):
            await provider.complete_json(system="s", prompt="p", schema=SCHEMA)
    by_name = {s["name"]: s for s in spans}
    outer, inner = by_name["outer"], by_name["inner"]
    llm = next(s for s in spans if s["kind"] == "llm")
    assert outer.get("parent_id") is None
    assert inner["parent_id"] == outer["id"] and llm["parent_id"] == inner["id"]
    assert llm["session_id"] == "s9" and llm["purpose"] == "evaluation"  # tags flow down


def test_match_score_compares_what_is_comparable():
    from services.observability.experiments import match_score

    expected = {"score": 7, "speak": True, "text": "x" * 80, "tags": ["a", "b"]}
    same = {"score": 8, "speak": True, "text": "anything else", "tags": ["a", "b"]}
    assert match_score(expected, same) == 1.0  # a point apart agrees; long text is ignored
    assert match_score(expected, {"score": 3, "speak": False, "tags": ["a"]}) < 0.5
    assert match_score({"text": "y" * 80}, {"text": "z"}) is None


async def test_a_saved_call_can_be_replayed_and_compared(client, host_auth, session_factory):
    from datetime import UTC, datetime

    from services.observability.models import Span

    async with session_factory() as db:
        span_row = Span(
            started_at=datetime.now(UTC), kind="llm", purpose="evaluation", agent="content",
            model="stub-deterministic", system_prompt="sys", prompt="judge this",
            output={"ok": True}, meta={"schema": SCHEMA},
        )  # fmt: skip
        db.add(span_row)
        await db.commit()
        span_id = str(span_row.id)
    base = "/api/v1/observability"
    saved = await client.post(
        f"{base}/datasets/cases", headers=host_auth, json={"span_id": span_id, "dataset": "d1"}
    )
    assert saved.status_code == 201 and saved.json()["expected"] == {"ok": True}
    assert (await client.get(f"{base}/datasets", headers=host_auth)).json() == [
        {"dataset": "d1", "cases": 1, "runs": 0}
    ]
    ran = await client.post(
        f"{base}/datasets/d1/run", headers=host_auth, json={"label": "baseline"}
    )
    assert ran.status_code == 200 and ran.json()["cases"] == 1 and ran.json()["errors"] == 0
    runs = (await client.get(f"{base}/datasets/d1/runs", headers=host_auth)).json()
    assert [r["label"] for r in runs] == ["baseline"]
    detail = await client.get(f"{base}/runs/{runs[0]['run_id']}", headers=host_auth)
    assert detail.json()[0]["expected"] == {"ok": True}
    case_id = saved.json()["id"]
    assert (
        await client.delete(f"{base}/datasets/cases/{case_id}", headers=host_auth)
    ).status_code == 204
    assert (await client.get(f"{base}/datasets", headers=host_auth)).json() == []


async def test_alerts_fire_on_errors_and_stay_quiet_when_healthy(
    client, host_auth, session_factory
):
    from datetime import UTC, datetime

    from services.observability.models import Span

    assert (await client.get("/api/v1/observability/alerts", headers=host_auth)).json() == []
    async with session_factory() as db:
        for _ in range(6):
            db.add(Span(started_at=datetime.now(UTC), kind="llm", purpose="persona",
                        model="stub", latency_ms=50, status="error"))  # fmt: skip
        await db.commit()
    alerts = (await client.get("/api/v1/observability/alerts", headers=host_auth)).json()
    assert any(a["title"] == "High error rate" for a in alerts)

    trace = await client.get("/api/v1/observability/trace/none", headers=host_auth)
    assert trace.json() == []
