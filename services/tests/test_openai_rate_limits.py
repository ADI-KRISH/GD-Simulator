"""Rate limits handled per model: move to a model with headroom, not sleep in lockstep.

Groq's free tier allows 8,000 tokens a minute per model. Five evaluators fired
together, all got 429 and all slept the same twenty seconds (2026-10-17). A
model that answers 429 now cools for the window the vendor names, for every
caller in the process, and calls move to the next configured model meanwhile —
one organization's models, each inside its own published limit.
"""

from types import SimpleNamespace

import pytest

from services.common.config import settings
from services.llm import openai_provider
from services.llm.openai_provider import OpenAIProvider

BIG, SMALL, OTHER = "openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"


class RateLimited(Exception):
    """The shape `_rate_limit_wait` reads: a status code and the vendor's wait."""

    status_code = 429

    def __init__(self, wait: str = "20"):
        super().__init__(f"Error code: 429 - rate limited, try again in {wait}s")
        self.response = SimpleNamespace(headers={"retry-after": wait})


class FakeCompletions:
    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []

    async def create(self, **request):
        self.requests.append(dict(request))
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _reply(content='{"ok": true}'):
    message = SimpleNamespace(content=content, tool_calls=None, refusal=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        model="m",
    )


def _provider(model, script):
    provider = OpenAIProvider(model=model, api_key="k")
    completions = FakeCompletions(script)
    provider._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
    return provider, completions


@pytest.fixture(autouse=True)
def clean_state(monkeypatch):
    saved = (settings.llm_model, settings.llm_fallback_models, settings.llm_reasoning_effort)
    openai_provider._COOLING_UNTIL.clear()
    slept: list[float] = []

    async def fake_sleep(seconds):
        slept.append(seconds)

    monkeypatch.setattr(openai_provider.asyncio, "sleep", fake_sleep)
    settings.llm_model, settings.llm_fallback_models = BIG, SMALL
    settings.llm_reasoning_effort = "low"
    yield slept
    openai_provider._COOLING_UNTIL.clear()
    settings.llm_model, settings.llm_fallback_models, settings.llm_reasoning_effort = saved


async def test_a_rate_limited_model_hands_the_call_to_the_fallback_at_once(clean_state):
    provider, completions = _provider(BIG, [RateLimited(), _reply()])
    await provider.complete_json(system="s", prompt="p", schema={"type": "object"})
    assert [r["model"] for r in completions.requests] == [BIG, SMALL]
    assert clean_state == [], "it moved models instead of sleeping"


async def test_the_next_call_skips_a_model_that_is_still_cooling(clean_state):
    first, _ = _provider(BIG, [RateLimited(), _reply()])
    await first.complete_json(system="s", prompt="p", schema={"type": "object"})
    second, completions = _provider(BIG, [_reply()])
    await second.complete_json(system="s", prompt="p", schema={"type": "object"})
    assert [r["model"] for r in completions.requests] == [SMALL]


async def test_when_every_model_is_cooling_the_call_waits_for_the_soonest(clean_state):
    now = openai_provider.time.monotonic()
    openai_provider._COOLING_UNTIL.update({BIG: now + 30, SMALL: now + 5})
    provider, completions = _provider(BIG, [_reply()])
    await provider.complete_json(system="s", prompt="p", schema={"type": "object"})
    assert completions.requests[0]["model"] == SMALL
    assert len(clean_state) == 1 and 0 < clean_state[0] <= 5


async def test_reasoning_effort_follows_the_model_actually_called(clean_state):
    settings.llm_fallback_models = OTHER
    provider, completions = _provider(BIG, [RateLimited(), _reply()])
    await provider.complete_json(system="s", prompt="p", schema={"type": "object"})
    assert completions.requests[0]["reasoning_effort"] == "low"
    assert completions.requests[1]["model"] == OTHER
    assert "reasoning_effort" not in completions.requests[1]


async def test_with_no_fallback_configured_a_429_still_waits_and_retries(clean_state):
    settings.llm_fallback_models = ""
    provider, completions = _provider(BIG, [RateLimited("7"), _reply()])
    await provider.complete_json(system="s", prompt="p", schema={"type": "object"})
    assert [r["model"] for r in completions.requests] == [BIG, BIG]
    assert len(clean_state) == 1 and 0 < clean_state[0] <= 7.5
