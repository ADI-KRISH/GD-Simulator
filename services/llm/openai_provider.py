"""OpenAI adapter for the LLM interface (design §54).

A sibling of `anthropic_provider.py`, and the reason both exist: claude.md
requires the architecture to stay vendor-independent, and a second adapter is
what makes that claim testable rather than aspirational. Adding this file
changed no agent, prompt, aggregator or contract.

Structured output uses the Chat Completions `response_format` JSON-schema mode.
Strict mode is used **where the schema qualifies** and non-strict where it does
not — see `_strict_compatible`. Either way the response still passes the
contract validation gate in `services.common.contracts` before anything
downstream sees it, so a non-strict response is checked, not trusted.
"""

import asyncio
import json
import logging
import random
import re
from typing import Any

from services.common.config import settings
from services.llm.base import LLMError, LLMResult, ToolDefinition, ToolInvocation

logger = logging.getLogger(__name__)


class OpenAIProvider:
    """Async OpenAI client behind the `LLMProvider` protocol."""

    name = "openai"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        self.model = model or settings.llm_model
        self._api_key = api_key or settings.llm_api_key or settings.openai_api_key or None
        self._timeout = timeout if timeout is not None else settings.llm_timeout_seconds
        self._max_retries = max_retries if max_retries is not None else settings.llm_max_retries
        self._client = None
        limit = settings.llm_max_concurrency
        self._slots = asyncio.Semaphore(limit) if limit > 0 else None

    async def _create(self, client, **request):
        """One chat completion, honouring the vendor's own rate-limit wait.

        Free tiers cap tokens per minute and say how long to wait. Sleeping that long
        (up to ten attempts, each capped at a minute) succeeds where the short generic
        backoff burns every retry inside the same window.
        """
        for attempt in range(10):
            try:
                if self._slots is None:
                    return await client.chat.completions.create(**request)
                async with self._slots:
                    return await client.chat.completions.create(**request)
            except Exception as exc:
                wait = _rate_limit_wait(exc)
                if wait is None or attempt == 9:
                    raise
                logger.warning(
                    "rate limited, waiting for the window",
                    extra={"wait": round(wait, 1), "attempt": attempt + 1},
                )
                await asyncio.sleep(wait)

    def _get_client(self):
        if self._client is None:
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover - dependency is pinned
                raise LLMError(
                    "the openai package is not installed; "
                    "set LLM_PROVIDER=stub or pip install openai"
                ) from exc

            kwargs: dict[str, Any] = {"timeout": self._timeout, "max_retries": 0}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            if settings.openai_base_url:
                kwargs["base_url"] = settings.openai_base_url
            self._client = AsyncOpenAI(**kwargs)
        return self._client

    async def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int | None = None,
    ) -> LLMResult:
        client = self._get_client()
        strict = _strict_compatible(schema)
        response_format = {
            "type": "json_schema",
            "json_schema": {
                # The name is required and must be a bare identifier.
                "name": schema.get("title", "response").replace(" ", "_").lower(),
                "schema": schema,
                "strict": strict,
            },
        }
        last_error: Exception | None = None

        for attempt in range(self._max_retries):
            try:
                response = await self._create(
                    client,
                    model=self.model,
                    max_completion_tokens=max_tokens or settings.llm_max_tokens,
                    messages=[
                        {"role": "system", "content": system},
                        {"role": "user", "content": prompt},
                    ],
                    response_format=response_format,
                )
                choice = response.choices[0]
                # A length-truncated response is invalid JSON, and retrying it
                # unchanged would truncate identically — fail loudly instead.
                if choice.finish_reason == "length":
                    raise LLMError(
                        f"response hit the {max_tokens or settings.llm_max_tokens} token cap "
                        "before completing its JSON"
                    )
                if choice.message.refusal:
                    raise LLMError(f"model refused: {choice.message.refusal}")

                text = choice.message.content or ""
                if not text.strip():
                    raise LLMError("model returned empty content")

                usage = response.usage
                return LLMResult(
                    data=_parse_json(text),
                    model=response.model,
                    input_tokens=usage.prompt_tokens if usage else 0,
                    output_tokens=usage.completion_tokens if usage else 0,
                    raw_text=text,
                    metadata={"finish_reason": choice.finish_reason, "strict": strict},
                )
            except json.JSONDecodeError as exc:
                last_error = exc
            except Exception as exc:
                last_error = exc
                if not _is_retryable(exc):
                    raise LLMError(f"openai request failed: {exc}") from exc

            if attempt < self._max_retries - 1:
                # Full jitter: several evaluator agents run concurrently for one
                # session, so a fixed backoff would retry them in lockstep and
                # re-trigger the same rate limit.
                delay = min(2**attempt + random.random(), 30.0)
                logger.warning(
                    "llm call failed, retrying",
                    extra={
                        "attempt": attempt + 1,
                        "max_retries": self._max_retries,
                        "delay": round(delay, 2),
                        "error": str(last_error),
                    },
                )
                await asyncio.sleep(delay)

        raise LLMError(
            f"openai request failed after {self._max_retries} attempts: {last_error}"
        ) from last_error

    async def complete_with_tools(
        self,
        *,
        system: str,
        prompt: str,
        tools: list[ToolDefinition],
        schema: dict[str, Any],
        max_tokens: int | None = None,
        max_rounds: int = 6,
    ) -> LLMResult:
        """Structured output with tool use.

        OpenAI accepts `tools` and `response_format` on the same request: while
        the model wants a tool it returns `tool_calls` and no content; when it
        is done it returns content matching the schema.
        """
        client = self._get_client()
        if settings.openai_base_url:
            # Other OpenAI-compatible vendors (e.g. Groq) reject response_format together
            # with tools, so the schema goes in the prompt; the contract gate still validates.
            system = (
                f"{system}\n\nWhen finished, reply with ONLY a JSON object matching this "
                f"JSON Schema, no prose:\n{json.dumps(schema)}"
            )
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        tool_specs = [
            {
                "type": "function",
                "function": {
                    "name": tool.name,
                    "description": tool.description,
                    "parameters": tool.input_schema,
                },
            }
            for tool in tools
        ]
        by_name = {tool.name: tool for tool in tools}
        invocations: list[ToolInvocation] = []
        input_tokens = output_tokens = 0

        for _round in range(max_rounds):
            try:
                request: dict[str, Any] = {
                    "model": self.model,
                    "max_completion_tokens": max_tokens or settings.llm_max_tokens,
                    "messages": messages,
                    "tools": tool_specs,
                }
                if not settings.openai_base_url:
                    request["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema.get("title", "response").replace(" ", "_").lower(),
                            "schema": schema,
                            "strict": _strict_compatible(schema),
                        },
                    }
                response = await self._create(client, **request)
            except Exception as exc:
                raise LLMError(f"openai tool request failed: {exc}") from exc

            message = response.choices[0].message
            if response.usage:
                input_tokens += response.usage.prompt_tokens
                output_tokens += response.usage.completion_tokens

            if message.tool_calls:
                messages.append(
                    {
                        "role": "assistant",
                        "content": message.content,
                        "tool_calls": [_echo_tool_call(call) for call in message.tool_calls],
                    }
                )
                for call in message.tool_calls:
                    tool = by_name.get(call.function.name)
                    try:
                        arguments = json.loads(call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                    result = (
                        await tool.invoke(arguments)
                        if tool
                        else {"error": f"no such tool: {call.function.name}"}
                    )
                    invocations.append(
                        ToolInvocation(name=call.function.name, arguments=arguments, result=result)
                    )
                    messages.append(
                        {
                            "role": "tool",
                            "tool_call_id": call.id,
                            "content": json.dumps(result, default=str),
                        }
                    )
                continue

            text = message.content or ""
            if not text.strip():
                raise LLMError("model returned neither a tool call nor content")
            try:
                data = _parse_json(text)
            except json.JSONDecodeError as exc:
                raise LLMError(f"tool-using response was not valid JSON: {exc}") from exc

            return LLMResult(
                data=data,
                model=response.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                raw_text=text,
                metadata={"rounds": _round + 1},
                tool_calls=invocations,
            )

        raise LLMError(f"model still requesting tools after {max_rounds} rounds")


def _echo_tool_call(call) -> dict[str, Any]:
    """A tool call as the assistant message that gets sent back to the model.

    Gemini's OpenAI-compatible endpoint attaches a `thought_signature` under
    `extra_content` and rejects the next turn with a 400 if it is not echoed back.
    The SDK keeps unknown fields in `model_extra`, so they are carried through.
    """
    echoed: dict[str, Any] = {
        "id": call.id,
        "type": "function",
        "function": {"name": call.function.name, "arguments": call.function.arguments},
    }
    extra = (getattr(call, "model_extra", None) or {}).get("extra_content")
    if extra:
        echoed["extra_content"] = extra
    return echoed


def _parse_json(text: str) -> Any:
    """json.loads, tolerating the markdown fence or lead-in prose some vendors add."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


def _rate_limit_wait(exc: Exception) -> float | None:
    """Seconds to wait if `exc` is an HTTP 429, else None."""
    if getattr(exc, "status_code", None) != 429:
        return None
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    retry_after = headers.get("retry-after")
    if retry_after:
        try:
            return min(float(retry_after) + 0.5, 65.0)
        except ValueError:
            pass
    match = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", str(exc))
    if match:
        return min(int(match.group(1) or 0) * 60 + float(match.group(2)) + 0.5, 65.0)
    return 10.0


def _strict_compatible(schema: dict[str, Any]) -> bool:
    """Whether a schema satisfies OpenAI's strict-mode subset.

    Strict mode guarantees the response validates, but it only accepts a subset
    of JSON Schema: every property must be listed in `required`, and every
    object must set `additionalProperties: false` — which rules out a
    string-keyed map. Of this project's contracts, `evaluation_output` qualifies
    and `rolling_state` does not (it has an optional `observations` field and a
    `candidate_positions` map).

    Claiming strict for a schema that does not qualify is a 400 from the API, so
    this is checked rather than assumed.
    """

    def check(node: Any) -> bool:
        if not isinstance(node, dict):
            return True
        if node.get("type") == "object":
            extra = node.get("additionalProperties", True)
            # A map (`additionalProperties: {schema}`) or an open object both
            # fail; only an explicit `false` is allowed.
            if extra is not False:
                return False
            properties = node.get("properties", {})
            if set(node.get("required", [])) != set(properties):
                return False
            return all(check(child) for child in properties.values())
        if node.get("type") == "array":
            return check(node.get("items", {}))
        return True

    return check(schema)


def _is_retryable(exc: Exception) -> bool:
    """Rate limits, timeouts, connection drops and 5xx are worth another go; a
    400 means the request itself is wrong and will fail identically."""
    try:
        import openai
    except ImportError:  # pragma: no cover
        return False

    if isinstance(exc, openai.RateLimitError | openai.APITimeoutError):
        return True
    if isinstance(exc, openai.APIConnectionError):
        return True
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code >= 500
    return isinstance(exc, LLMError)
