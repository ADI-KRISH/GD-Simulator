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
        """Take the scripted replies/errors to hand out in order."""
        self.script = list(script)
        self.requests: list[dict] = []

    async def create(self, **request):
        """Record the request, then return or raise the next scripted step."""
        self.requests.append(request)
        step = self.script.pop(0)
        if isinstance(step, Exception):
            raise step
        return step


def _client(script):
    """A fake OpenAI client plus the completions object it records into."""
    completions = FakeCompletions(script)
    return SimpleNamespace(chat=SimpleNamespace(completions=completions)), completions


def _reply(content=None, tool_calls=None):
    """A fake chat completion with the given content and tool calls."""
    message = SimpleNamespace(content=content, tool_calls=tool_calls, refusal=None)
    return SimpleNamespace(
        choices=[SimpleNamespace(message=message, finish_reason="stop")],
        usage=SimpleNamespace(prompt_tokens=10, completion_tokens=5),
        model="openai/gpt-oss-120b",
    )


@pytest.fixture(autouse=True)
def restore_settings():
    """Restore the LLM settings these tests change."""
    saved = (settings.openai_base_url, settings.llm_reasoning_effort)
    yield
    settings.openai_base_url, settings.llm_reasoning_effort = saved


def test_only_the_reasoning_models_are_given_an_effort():
    """Only gpt-oss models count as 'think first' models."""
    assert _thinks_first("openai/gpt-oss-120b")
    assert _thinks_first("openai/gpt-oss-20b")
    assert not _thinks_first("gpt-4o-mini")
    assert not _thinks_first("qwen/qwen3.8-27b")


async def test_a_gpt_oss_request_carries_the_configured_effort():
    """A gpt-oss request includes the configured reasoning effort."""
    settings.llm_reasoning_effort = "low"
    provider = OpenAIProvider(model="openai/gpt-oss-20b", api_key="k")
    provider._client, completions = _client([_reply('{"speak": true, "text": "hi"}')])
    result = await provider.complete_json(system="s", prompt="p", schema={"type": "object"})
    assert result.data == {"speak": True, "text": "hi"}
    assert completions.requests[0]["reasoning_effort"] == "low"


async def test_other_models_are_sent_no_effort():
    """Other models get no reasoning_effort field."""
    settings.llm_reasoning_effort = "low"
    provider = OpenAIProvider(model="gpt-4o-mini", api_key="k")
    provider._client, completions = _client([_reply('{"ok": true}')])
    await provider.complete_json(system="s", prompt="p", schema={"type": "object"})
    assert "reasoning_effort" not in completions.requests[0]


async def test_an_empty_setting_sends_no_effort_even_to_gpt_oss():
    """An empty effort setting sends nothing, even to gpt-oss."""
    settings.llm_reasoning_effort = ""
    provider = OpenAIProvider(model="openai/gpt-oss-120b", api_key="k")
    provider._client, completions = _client([_reply('{"ok": true}')])
    await provider.complete_json(system="s", prompt="p", schema={"type": "object"})
    assert "reasoning_effort" not in completions.requests[0]


async def test_an_answer_sent_as_an_undeclared_tool_is_asked_for_again_as_content():
    """A refused 'json' tool call is retried with tools off to get plain content."""
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
    """A non tool-routing failure (e.g. 401) still raises LLMError."""
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
    """A dropped connection is retried instead of failing the call."""
    import httpx
    import openai

    from services.llm import openai_provider

    async def no_wait(_seconds):
        """Skip the real backoff sleep."""
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
    """Invalid JSON triggers exactly one follow-up asking for valid JSON."""
    settings.openai_base_url = "https://api.groq.com/openai/v1"
    provider = OpenAIProvider(model="openai/gpt-oss-120b", api_key="k")
    provider._client, completions = _client([_reply('{"score": 6 "x": 1}'), _reply('{"score": 6}')])
    result = await provider.complete_with_tools(
        system="s", prompt="p", tools=[], schema={"type": "object"}
    )
    assert result.data == {"score": 6}
    assert "not valid JSON" in completions.requests[1]["messages"][-1]["content"]


class Refused(Exception):
    """Groq's tool_use_failed, with the call it refused in `failed_generation`."""

    def __init__(self, generation: str):
        """Build a tool_use_failed error carrying the refused generation."""
        super().__init__("Error code: 400 - {'error': {'code': 'tool_use_failed'}}")
        self.body = {"error": {"code": "tool_use_failed", "failed_generation": generation}}


async def test_an_answer_sent_to_an_undeclared_json_tool_is_taken_as_the_answer():
    """A refused call to an undeclared 'json' tool is used as the final answer."""
    settings.openai_base_url = "https://api.groq.com/openai/v1"
    provider = OpenAIProvider(model="openai/gpt-oss-120b", api_key="k")
    refused = Refused('{"name": "json", "arguments": {"score": 7, "evidence": ["x"]}}')
    provider._client, completions = _client([refused])
    result = await provider.complete_with_tools(
        system="s", prompt="p", tools=[], schema={"type": "object"}
    )
    assert result.data == {"score": 7, "evidence": ["x"]}
    assert len(completions.requests) == 1, "no second call needed"


async def test_a_real_tool_the_vendor_refused_is_run_as_meant():
    """A refused call to a real tool is still executed and its result fed back."""
    from services.llm.base import ToolDefinition

    ran: list[dict] = []

    def score_if(dimension, hypothetical_score):
        """Fake tool that records its arguments and returns a score."""
        ran.append({"dimension": dimension, "score": hypothetical_score})
        return {"overall": 71}

    tool = ToolDefinition(
        name="score_if", description="d", input_schema={"type": "object"}, handler=score_if
    )
    settings.openai_base_url = "https://api.groq.com/openai/v1"
    provider = OpenAIProvider(model="openai/gpt-oss-120b", api_key="k")
    refused = Refused(
        '{"name": "score_if", "arguments": {"dimension": "leadership", "hypothetical_score": 8}}'
    )
    provider._client, completions = _client([refused, _reply('{"summary": "ok"}')])
    result = await provider.complete_with_tools(
        system="s", prompt="p", tools=[tool], schema={"type": "object"}
    )
    assert ran == [{"dimension": "leadership", "score": 8}]
    assert result.data == {"summary": "ok"}
    assert result.tool_calls[0].name == "score_if"
    assert completions.requests[1]["messages"][-1]["role"] == "tool"
