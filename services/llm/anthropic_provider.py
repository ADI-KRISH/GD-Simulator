"""Anthropic adapter for the LLM interface (design §54).

Purpose:     Talks to Claude through the Anthropic SDK and presents it as an
             `LLMProvider`: JSON-schema constrained output, retries with jittered
             backoff, and a tool-use loop.
Why needed:  Production-quality evaluation needs a real model. Keeping the SDK
             inside this one file means the rest of the code never changes when
             the vendor does.
Contains:
  - `AnthropicProvider`       the adapter class.
      - `_get_client`           builds the SDK client lazily (import only if used).
      - `complete_json`         one structured call, retried on transient errors.
      - `complete_with_tools`   the call -> run tools -> feed results back loop.
  - `_is_retryable`           decides which failures are worth another attempt.
Used by:     llm/__init__.py (imported lazily when `LLM_PROVIDER=anthropic`, or
             for the persona model).
Depends on:  common/config.py (model, key, timeout, retries), llm/base.py.

The only file in the project that imports a model vendor's SDK. Swapping
providers means writing a sibling of this class — no agent, prompt, or
aggregator changes.

Structured output uses `output_config.format` with the JSON Schema, so the model
returns JSON valid against the contract by construction rather than by asking it
nicely in the prompt. The result still passes the contract validation gate in
`services.common.contracts` before anything downstream sees it — a schema-shaped
response can still be semantically wrong, and claude.md requires validation at
the boundary regardless.
"""

import asyncio  # sleep between retries
import json  # parse the model's JSON text; serialise tool results
import logging  # retry warnings
import random  # jitter for retry backoff
from typing import Any  # JSON schema / message types

from services.common.config import settings  # model, key, timeout, retries, token cap
from services.llm.base import LLMError, LLMResult, ToolDefinition, ToolInvocation  # interface

logger = logging.getLogger(__name__)


class AnthropicProvider:
    """Async Anthropic client behind the `LLMProvider` protocol."""

    name = "anthropic"

    def __init__(
        self,
        model: str | None = None,
        api_key: str | None = None,
        *,
        timeout: float | None = None,
        max_retries: int | None = None,
    ) -> None:
        # Explicit arguments win; otherwise fall back to values from settings/.env.
        self.model = model or settings.llm_model
        self._api_key = api_key or settings.llm_api_key or settings.anthropic_api_key or None
        self._timeout = timeout if timeout is not None else settings.llm_timeout_seconds
        self._max_retries = max_retries if max_retries is not None else settings.llm_max_retries
        self._client = None

    def _get_client(self):
        """Create the AsyncAnthropic client on first use (the SDK import is lazy)."""
        if self._client is None:
            # Import here so the SDK is only required when this provider is used.
            try:
                from anthropic import AsyncAnthropic
            except ImportError as exc:  # pragma: no cover - dependency is pinned
                raise LLMError(
                    "the anthropic package is not installed; "
                    "set LLM_PROVIDER=stub or install services/requirements.txt"
                ) from exc

            # The SDK resolves the key from the environment when none is passed,
            # so an unset LLM_API_KEY is not automatically an error.
            kwargs: dict[str, Any] = {"timeout": self._timeout, "max_retries": 0}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            self._client = AsyncAnthropic(**kwargs)
        return self._client

    async def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int | None = None,
    ) -> LLMResult:
        """Ask Claude for JSON matching `schema`, retrying transient failures with backoff."""
        client = self._get_client()
        last_error: Exception | None = None

        # Retries are handled here rather than by the SDK so that a malformed
        # (schema-valid-but-unparseable) response is retried on the same terms
        # as a transport failure — both are "the model did not give us usable
        # JSON", and both are recoverable.
        for attempt in range(self._max_retries):
            try:
                # Send the request with the JSON schema attached as the output format.
                response = await client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens or settings.llm_max_tokens,
                    system=system,
                    messages=[{"role": "user", "content": prompt}],
                    output_config={"format": {"type": "json_schema", "schema": schema}},
                )
                # Pull the first text block out of the response.
                text = next(
                    (b.text for b in response.content if getattr(b, "type", None) == "text"), ""
                )
                if not text:
                    raise LLMError("model returned no text content")
                # Parse and return; token counts are kept for cost reporting.
                return LLMResult(
                    data=json.loads(text),
                    model=response.model,
                    input_tokens=response.usage.input_tokens,
                    output_tokens=response.usage.output_tokens,
                    raw_text=text,
                    metadata={"stop_reason": response.stop_reason},
                )
            except json.JSONDecodeError as exc:
                # Unparseable JSON: remember it and try again.
                last_error = exc
            except Exception as exc:
                last_error = exc
                # A non-transient failure (e.g. a 400) will fail the same way, so stop now.
                if not _is_retryable(exc):
                    raise LLMError(f"anthropic request failed: {exc}") from exc

            # Wait before the next attempt (but not after the last one).
            if attempt < self._max_retries - 1:
                # Full jitter: several evaluator agents run concurrently for the
                # same session, so a fixed backoff would have them all retry in
                # lockstep and re-trigger the same rate limit.
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

        # Every attempt failed: surface one clean LLMError to the caller.
        raise LLMError(
            f"anthropic request failed after {self._max_retries} attempts: {last_error}"
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

        Anthropic's shape differs from OpenAI's: tool requests arrive as
        `tool_use` content blocks with `stop_reason == "tool_use"`, and results
        go back as `tool_result` blocks in a user message. The whole assistant
        turn must be echoed back unchanged, not just the tool call.
        """
        client = self._get_client()
        # The running conversation; grows by an assistant + user turn per tool round.
        messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
        # Translate our ToolDefinitions into the shape the Anthropic API expects.
        tool_specs = [
            {
                "name": tool.name,
                "description": tool.description,
                "input_schema": tool.input_schema,
            }
            for tool in tools
        ]
        # Lookup table so a tool request from the model can be routed to its handler.
        by_name = {tool.name: tool for tool in tools}
        invocations: list[ToolInvocation] = []
        input_tokens = output_tokens = 0

        # Each round is one model call; stop when it answers instead of asking for a tool.
        for _round in range(max_rounds):
            try:
                response = await client.messages.create(
                    model=self.model,
                    max_tokens=max_tokens or settings.llm_max_tokens,
                    system=system,
                    messages=messages,
                    tools=tool_specs,
                    output_config={"format": {"type": "json_schema", "schema": schema}},
                )
            except Exception as exc:
                raise LLMError(f"anthropic tool request failed: {exc}") from exc

            # Token usage accumulates across rounds so the cost reflects the whole loop.
            input_tokens += response.usage.input_tokens
            output_tokens += response.usage.output_tokens

            # The model asked for tools: run them and send the results back.
            if response.stop_reason == "tool_use":
                # Echo the assistant turn back verbatim before the results.
                messages.append({"role": "assistant", "content": response.content})
                results = []
                for block in response.content:
                    if getattr(block, "type", None) != "tool_use":
                        continue
                    tool = by_name.get(block.name)
                    arguments = dict(block.input or {})
                    # Run our handler; an unknown tool name is reported back as an error.
                    result = (
                        await tool.invoke(arguments)
                        if tool
                        else {"error": f"no such tool: {block.name}"}
                    )
                    # Keep an audit record of what was asked and answered.
                    invocations.append(
                        ToolInvocation(name=block.name, arguments=arguments, result=result)
                    )
                    results.append(
                        {
                            "type": "tool_result",
                            "tool_use_id": block.id,
                            "content": json.dumps(result, default=str),
                        }
                    )
                messages.append({"role": "user", "content": results})
                continue

            # No tool request: this is the final answer. Extract its text.
            text = next(
                (b.text for b in response.content if getattr(b, "type", None) == "text"), ""
            )
            if not text.strip():
                raise LLMError("model returned neither a tool call nor content")
            try:
                data = json.loads(text)
            except json.JSONDecodeError as exc:
                raise LLMError(f"tool-using response was not valid JSON: {exc}") from exc

            return LLMResult(
                data=data,
                model=response.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                raw_text=text,
                metadata={"rounds": _round + 1, "stop_reason": response.stop_reason},
                tool_calls=invocations,
            )

        # The loop bound was hit: a model that never stops asking for tools must not hang a worker.
        raise LLMError(f"model still requesting tools after {max_rounds} rounds")


def _is_retryable(exc: Exception) -> bool:
    """Rate limits, timeouts, connection drops and 5xx are worth another go;
    a 400 means the request itself is wrong and will fail identically."""
    try:
        import anthropic
    except ImportError:  # pragma: no cover
        return False

    # Transient server/network conditions: retry. Client errors (4xx): don't.
    if isinstance(exc, anthropic.RateLimitError | anthropic.APITimeoutError):
        return True
    if isinstance(exc, anthropic.APIConnectionError):
        return True
    if isinstance(exc, anthropic.APIStatusError):
        return exc.status_code >= 500
    return isinstance(exc, LLMError)
