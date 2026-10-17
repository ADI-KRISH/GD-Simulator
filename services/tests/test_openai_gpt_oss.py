"""gpt-oss on Groq: the two behaviours that silenced practice and failed every evaluator.

Found by running a real session on 2026-10-17, when Groq's account served only
gpt-oss: (1) with no reasoning effort the model spends a 150-token persona line
on hidden reasoning and returns nothing, which Groq reports as
`json_validate_failed`; (2) on its final round it delivers the answer as a call
to a tool named `json`, which Groq rejects as `tool_use_failed`.

The transport is a fake with the client's shape, so these pin the request the
adapter sends, not Groq's behaviour.
"""

from types import SimpleNamespace

import pytest

from services.common.config import settings
from services.llm.openai_provider import OpenAIProvider, _thinks_first


class FakeCompletions:
    """Records each request; raises or answers from a script, in order."""

    def __init__(self, script):
        self.script = list(script)
        self.requests: list[dict] = []

    async def create(self, **request):
        self.requests.append(request)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _client(script):
    completions = FakeCompletions(script)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def _reply(content=None, tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls, refusal=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        model="openai/gpt-oss-120b",
    )


@pytest.fixture(autouse=True)
def restore_settings():
    saved = (settings.openai_base_url, settings.llm_reasoning_effort)
    yield
    settings.openai_base_url, settings.llm_reasoning_effort = saved


def test_only_the_reasoning_models_are_given_an_effort():
    assert _thinks_first("openai/gpt-oss-120b")
    assert _thinks_first("openai/gpt-oss-20b")
    assert not _thinks_first("gpt-4o-mini")
    assert not _thinks_first("qwen/qwen3.8-27b")


async def test_a_gpt_oss_request_carries_the_configured_effort():
    settings.llm_reasoning_effort = "low"
    provider = OpenAIProvider(model="openai/gpt-oss-20b", api_key="k")
    provider._client, completions = _client([_reply('{"speak": true, "text": "hi"}')])
    result = await provider.complete_json(system="s", prompt="p", schema={"type": "object"})
    assert result.data == {"speak": True, "text": "hi"}
    assert completions.requests[0]["reasoning_effort"] == "low"


async def test_other_models_are_sent_no_effort():
    settings.llm_reasoning_effort = "low"
    provider = OpenAIProvider(model="gpt-4o-mini", api_key="k")
    provider._client, completions = _client([_reply('{"ok": true}')])
    await provider.complete_json(system="s", prompt="p", schema={"type": "object"})
    assert "reasoning_effort" not in completions.requests[0]


async def test_an_empty_setting_sends_no_effort_even_to_gpt_oss():
    settings.llm_reasoning_effort = ""
    provider = OpenAIProvider(model="openai/gpt-oss-120b", api_key="k")
    provider._client, completions = _client([_reply('{"ok": true}')])
    await provider.complete_json(system="s", prompt="p", schema={"type": "object"})
    assert "reasoning_effort" not in completions.requests[0]


async def test_an_answer_sent_as_an_undeclared_tool_is_asked_for_again_as_content():
    settings.openai_base_url = "https://api.groq.com/openai/v1"
    provider = OpenAIProvider(model="openai/gpt-oss-120b", api_key="k")
    rejected = Exception(
        "Error code: 400 - {'error': {'message': \"Tool call validation failed: attempted to "
        "call tool 'json' which was not in request.tools\", 'code': 'tool_use_failed'}}"
    )
    provider._client, completions = _client([rejected, _reply('{"score": 7}')])
    result = await provider.complete_with_tools(
        system="s", prompt="p", tools=[], schema={"type": "object"}
    )
    assert result.data == {"score": 7}
    assert "tool_choice" not in completions.requests[0]
    assert completions.requests[1]["tool_choice"] == "none"


async def test_any_other_failure_is_still_an_error():
    from services.llm.base import LLMError

    settings.openai_base_url = "https://api.groq.com/openai/v1"
    provider = OpenAIProvider(model="openai/gpt-oss-120b", api_key="k")
    provider._client, completions = _client([Exception("Error code: 401 - invalid_api_key")])
    with pytest.raises(LLMError):
        await provider.complete_with_tools(
            system="s", prompt="p", tools=[], schema={"type": "object"}
        )
    assert len(completions.requests) == 1


async def test_a_dropped_connection_is_retried_not_failed(monkeypatch):
    import httpx
    import openai

    from services.llm import openai_provider

    async def no_wait(_seconds):
        return None

    monkeypatch.setattr(openai_provider.asyncio, "sleep", no_wait)
    settings.openai_base_url = "https://api.groq.com/openai/v1"
    provider = OpenAIProvider(model="openai/gpt-oss-120b", api_key="k")
    dropped = openai.APIConnectionError(request=httpx.Request("POST", "https://api.groq.com"))
    provider._client, completions = _client([dropped, _reply('{"score": 6}')])
    result = await provider.complete_with_tools(
        system="s", prompt="p", tools=[], schema={"type": "object"}
    )
    assert result.data == {"score": 6}
    assert len(completions.requests) == 2


async def test_a_verdict_with_broken_json_is_asked_for_once_more():
    settings.openai_base_url = "https://api.groq.com/openai/v1"
    provider = OpenAIProvider(model="openai/gpt-oss-120b", api_key="k")
    provider._client, completions = _client([_reply('{"score": 6 "x": 1}'), _reply('{"score": 6}')])
    result = await provider.complete_with_tools(
        system="s", prompt="p", tools=[], schema={"type": "object"}
    )
    assert result.data == {"score": 6}
    assert "not valid JSON" in completions.requests[1]["messages"][-1]["content"]
