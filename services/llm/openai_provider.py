"""OpenAI adapter for the LLM interface (design §54).

Purpose:     Talks to OpenAI (or any OpenAI-compatible vendor such as Groq or
             Gemini via `OPENAI_BASE_URL`) and presents it as an `LLMProvider`.
             Handles JSON-schema output, per-model rate-limit cooldowns with
             fallback models, retries, and a tool-use loop that survives the
             quirks of compatible vendors.
Why needed:  A second real adapter is what proves the system is vendor
             independent, and free-tier vendors need the rate-limit handling to
             be usable at all.
Contains:
  - `_COOLING_UNTIL`, `_model_with_headroom`   process-wide rate-limit memory:
        which models are cooling and which one to call next.
  - `OpenAIProvider`       the adapter class.
      - `_create`            one chat completion that honours 429 waits and
                             moves to a fallback model.
      - `_tool_round`        one tool-using round, recovering vendor refusals.
      - `_candidate_models`  ordering of the model plus configured fallbacks.
      - `_get_client`        lazy SDK client construction.
      - `complete_json`      structured call with retries.
      - `complete_with_tools` the call -> run tools -> feed results back loop.
  - `_echo_tool_call`      replays a tool call to the vendor in the form it needs.
  - `_parse_json`          JSON parsing that tolerates fences / lead-in prose.
  - `MisroutedToolCall`, `_refused_tool_call`  recover a tool call the vendor
        refused to route (Groq `tool_use_failed`).
  - `_thinks_first`        detects reasoning models that take `reasoning_effort`.
  - `_rate_limit_wait`     reads how long a 429 says to wait.
  - `_strict_compatible`   whether a schema may use OpenAI strict mode.
  - `_is_retryable`        which failures deserve another attempt.
Used by:     llm/__init__.py (imported lazily when `LLM_PROVIDER=openai`, or for
             the persona model) and tests/test_llm_providers.py,
             test_openai_gpt_oss.py, test_openai_rate_limits.py,
             test_openai_tool_echo.py.
Depends on:  common/config.py (model, key, base URL, limits, fallbacks),
             llm/base.py (interface and result types).

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

import asyncio  # semaphore for concurrency limit; sleeps while rate limited
import json  # parse model JSON; serialise tool arguments/results
import logging  # rate-limit and retry warnings
import random  # jitter for retry backoff
import re  # parse "try again in Xs" out of 429 messages
import time  # monotonic clock for model cooldowns
from typing import Any  # JSON schema / message types

from services.common.config import settings  # model, key, base URL, limits, fallbacks
from services.llm.base import LLMError, LLMResult, ToolDefinition, ToolInvocation  # interface

logger = logging.getLogger(__name__)

#: model id -> monotonic time its rate-limit window reopens. Shared by every
#: provider in the process, because the vendor's limit is per model, not per
#: caller: the personas and the evaluators draw on the same buckets.
_COOLING_UNTIL: dict[str, float] = {}


async def _model_with_headroom(models: list[str]) -> str:
    """The first model not cooling; if all are, wait for the soonest to reopen."""
    now = time.monotonic()
    # Prefer the earliest model in priority order that is not rate limited.
    for model in models:
        if _COOLING_UNTIL.get(model, 0.0) <= now:
            return model
    # Every model is cooling: sleep until the one that reopens first.
    soonest = min(models, key=lambda m: _COOLING_UNTIL.get(m, 0.0))
    await asyncio.sleep(max(_COOLING_UNTIL[soonest] - now, 0.0))
    return soonest


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
        # Explicit arguments win; otherwise fall back to values from settings/.env.
        self.model = model or settings.llm_model
        self._api_key = api_key or settings.llm_api_key or settings.openai_api_key or None
        self._timeout = timeout if timeout is not None else settings.llm_timeout_seconds
        self._max_retries = max_retries if max_retries is not None else settings.llm_max_retries
        self._client = None
        # Optional cap on simultaneous requests (0 = unlimited) to stay inside vendor limits.
        limit = settings.llm_max_concurrency
        self._slots = asyncio.Semaphore(limit) if limit > 0 else None

    async def _create(self, client, **request):
        """One chat completion, honouring the vendor's own rate-limit wait.

        Free tiers cap tokens per minute **per model** and say how long to wait. A
        model that answers 429 is marked as cooling for that long, for every caller
        in the process; the call moves to the next model with headroom
        (`LLM_FALLBACK_MODELS`) and only sleeps when every model is cooling. The
        evaluators used to fire together, all get 429 and all sleep the same 20 s.
        One organization's models, each inside its own published limit.
        """
        models = self._candidate_models(request.get("model") or self.model)
        for attempt in range(10):
            # Choose a model that is not currently rate limited.
            request["model"] = await _model_with_headroom(models)
            # Only reasoning models accept `reasoning_effort`; strip it for the rest.
            if _thinks_first(request["model"]) and settings.llm_reasoning_effort:
                request["reasoning_effort"] = settings.llm_reasoning_effort
            else:
                request.pop("reasoning_effort", None)
            try:
                # Send the request, within the concurrency cap if one is set.
                if self._slots is None:
                    return await client.chat.completions.create(**request)
                async with self._slots:
                    return await client.chat.completions.create(**request)
            except Exception as exc:
                wait = _rate_limit_wait(exc)
                # Not a 429 (or out of attempts): let the caller deal with it.
                if wait is None or attempt == 9:
                    raise
                # 429: mark this model as cooling so every caller avoids it, then loop.
                _COOLING_UNTIL[request["model"]] = time.monotonic() + wait
                logger.warning(
                    "rate limited; model cooling",
                    extra={
                        "model": request["model"],
                        "wait": round(wait, 1),
                        "attempt": attempt + 1,
                    },
                )

    async def _tool_round(self, client, request: dict[str, Any]):
        """One round of a tool-using call, surviving the failures seen live.

        gpt-oss on Groq makes tool calls Groq refuses to route (tool_use_failed):
        a call to a tool named "json" carrying its final answer, or a call to a
        real tool after being told to call none. Groq returns the call it refused
        in `failed_generation`; that is raised as `MisroutedToolCall` for the loop
        to honour — run the tool, or take the arguments as the answer. With no
        readable `failed_generation`, the round is asked again with tools off.
        A dropped connection or a 5xx is retried twice, briefly.
        """
        for attempt in range(3):
            try:
                try:
                    return await self._create(client, **request)
                except Exception as exc:
                    # Anything other than the vendor's "tool_use_failed" goes to the outer handler.
                    if "tool_use_failed" not in str(exc):
                        raise
                    # The refused call is recoverable: hand it to the loop to honour.
                    if call := _refused_tool_call(exc):
                        raise call from exc
                    logger.info("model called a tool the vendor refused; asking for content")
                    # Nothing readable to recover: ask again with tools switched off.
                    try:
                        return await self._create(client, **request, tool_choice="none")
                    except Exception as again:
                        if call := _refused_tool_call(again):
                            raise call from again
                        raise
            except MisroutedToolCall:
                # Recovered intent is not an error; let complete_with_tools handle it.
                raise
            except Exception as exc:
                # Dropped connections and 5xx get two brief retries; anything else fails now.
                if attempt == 2 or not _is_retryable(exc):
                    raise
                logger.warning(
                    "tool round failed, retrying", extra={"attempt": attempt + 1, "error": str(exc)}
                )
                await asyncio.sleep(0.5 * 3**attempt)

    def _candidate_models(self, first: str) -> list[str]:
        """This call's model, then the configured fallbacks, then the main model."""
        fallbacks = [m.strip() for m in settings.llm_fallback_models.split(",") if m.strip()]
        ordered: list[str] = []
        # Priority order, de-duplicated while preserving it.
        for model in (first, *fallbacks, settings.llm_model):
            if model and model not in ordered:
                ordered.append(model)
        return ordered

    def _get_client(self):
        """Create the AsyncOpenAI client on first use (works with any compatible base URL)."""
        if self._client is None:
            # Import here so the SDK is only required when this provider is used.
            try:
                from openai import AsyncOpenAI
            except ImportError as exc:  # pragma: no cover - dependency is pinned
                raise LLMError(
                    "the openai package is not installed; "
                    "set LLM_PROVIDER=stub or pip install openai"
                ) from exc

            # SDK retries are off; this class does its own so behaviour is uniform.
            kwargs: dict[str, Any] = {"timeout": self._timeout, "max_retries": 0}
            if self._api_key:
                kwargs["api_key"] = self._api_key
            # A custom base URL points the same client at Groq, Gemini, etc.
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
        """Ask for JSON matching `schema` via json_schema response_format, with retries."""
        client = self._get_client()
        # Use strict mode only when the schema is in the subset OpenAI accepts.
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
                # Send the request (rate-limit handling lives in `_create`).
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
                # The model declined to answer: not retryable, report it.
                if choice.message.refusal:
                    raise LLMError(f"model refused: {choice.message.refusal}")

                text = choice.message.content or ""
                if not text.strip():
                    raise LLMError("model returned empty content")

                # Parse and return; token counts are kept for cost reporting.
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
                # Unparseable JSON: remember it and try again.
                last_error = exc
            except Exception as exc:
                last_error = exc
                # A non-transient failure will fail the same way, so stop now.
                if not _is_retryable(exc):
                    raise LLMError(f"openai request failed: {exc}") from exc

            # Wait before the next attempt (but not after the last one).
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

        # Every attempt failed: surface one clean LLMError to the caller.
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
        # The running conversation; grows by an assistant + tool turn per round.
        messages: list[dict[str, Any]] = [
            {"role": "system", "content": system},
            {"role": "user", "content": prompt},
        ]
        # Translate our ToolDefinitions into OpenAI's "function" tool format.
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
        # Lookup table so a tool request from the model can be routed to its handler.
        by_name = {tool.name: tool for tool in tools}
        invocations: list[ToolInvocation] = []
        input_tokens = output_tokens = 0
        # Allow one "that wasn't valid JSON, try again" nudge per call.
        bad_json_retried = False

        # Each round is one model call; stop when it answers instead of asking for a tool.
        for _round in range(max_rounds):
            try:
                # Build this round's request from the current conversation.
                request: dict[str, Any] = {
                    "model": self.model,
                    "max_completion_tokens": max_tokens or settings.llm_max_tokens,
                    "messages": messages,
                    "tools": tool_specs,
                }
                # Only genuine OpenAI accepts a response_format alongside tools.
                if not settings.openai_base_url:
                    request["response_format"] = {
                        "type": "json_schema",
                        "json_schema": {
                            "name": schema.get("title", "response").replace(" ", "_").lower(),
                            "schema": schema,
                            "strict": _strict_compatible(schema),
                        },
                    }
                response = await self._tool_round(client, request)
            except MisroutedToolCall as call:
                tool = by_name.get(call.name)
                if tool is None:
                    # A call to an undeclared tool ("json") carries the final answer.
                    return LLMResult(
                        data=call.arguments,
                        model=request["model"],
                        input_tokens=input_tokens,
                        output_tokens=output_tokens,
                        raw_text=json.dumps(call.arguments),
                        metadata={"rounds": _round + 1, "recovered": call.name},
                        tool_calls=invocations,
                    )
                # A real tool the vendor refused to route: run it, as it was meant.
                result = await tool.invoke(call.arguments)
                invocations.append(
                    ToolInvocation(name=call.name, arguments=call.arguments, result=result)
                )
                # Replay the call and its result into the conversation so the model sees them.
                call_id = f"recovered_{_round}"
                messages.append(
                    {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [
                            {
                                "id": call_id,
                                "type": "function",
                                "function": {
                                    "name": call.name,
                                    "arguments": json.dumps(call.arguments),
                                },
                            }
                        ],
                    }
                )
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call_id,
                        "content": json.dumps(result, default=str),
                    }
                )
                continue
            except Exception as exc:
                raise LLMError(f"openai tool request failed: {exc}") from exc

            message = response.choices[0].message
            # Token usage accumulates across rounds so the cost reflects the whole loop.
            if response.usage:
                input_tokens += response.usage.prompt_tokens
                output_tokens += response.usage.completion_tokens

            # The model asked for tools: run them and send the results back.
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
                    # Malformed argument JSON becomes "no arguments", so the tool errors cleanly.
                    try:
                        arguments = json.loads(call.function.arguments or "{}")
                    except json.JSONDecodeError:
                        arguments = {}
                    # Run our handler; an unknown tool name is reported back as an error.
                    result = (
                        await tool.invoke(arguments)
                        if tool
                        else {"error": f"no such tool: {call.function.name}"}
                    )
                    # Keep an audit record of what was asked and answered.
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

            # No tool request: this is the final answer. Check it has content.
            text = message.content or ""
            if not text.strip():
                raise LLMError("model returned neither a tool call nor content")
            try:
                data = _parse_json(text)
            except json.JSONDecodeError as exc:
                if bad_json_retried:
                    raise LLMError(f"tool-using response was not valid JSON: {exc}") from exc
                # Asked once more: a dropped comma in a long verdict should not cost
                # the candidate a whole dimension.
                bad_json_retried = True
                messages.append({"role": "assistant", "content": text})
                messages.append(
                    {
                        "role": "user",
                        "content": "That reply was not valid JSON. Reply again with ONLY the "
                        "JSON object, matching the schema exactly.",
                    }
                )
                continue

            return LLMResult(
                data=data,
                model=response.model,
                input_tokens=input_tokens,
                output_tokens=output_tokens,
                raw_text=text,
                metadata={"rounds": _round + 1},
                tool_calls=invocations,
            )

        # The loop bound was hit: a model that never stops asking for tools must not hang a worker.
        raise LLMError(f"model still requesting tools after {max_rounds} rounds")


def _echo_tool_call(call) -> dict[str, Any]:
    """A tool call as the assistant message that gets sent back to the model.

    Gemini's OpenAI-compatible endpoint attaches a `thought_signature` under
    `extra_content` and rejects the next turn with a 400 if it is not echoed back.
    The SDK keeps unknown fields in `model_extra`, so they are carried through.
    """
    # The standard fields every OpenAI-compatible endpoint expects.
    echoed: dict[str, Any] = {
        "id": call.id,
        "type": "function",
        "function": {"name": call.function.name, "arguments": call.function.arguments},
    }
    # Vendor-specific extras (Gemini's thought signature) must be passed back unchanged.
    extra = (getattr(call, "model_extra", None) or {}).get("extra_content")
    if extra:
        echoed["extra_content"] = extra
    return echoed


def _parse_json(text: str) -> Any:
    """json.loads, tolerating the markdown fence or lead-in prose some vendors add."""
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        # Fall back to the outermost {...} span, dropping any fence or chatter around it.
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise
        return json.loads(text[start : end + 1])


class MisroutedToolCall(Exception):
    """A tool call the vendor refused to route; the model's intent, recovered."""

    def __init__(self, name: str, arguments: dict[str, Any]) -> None:
        super().__init__(f"refused tool call: {name}")
        self.name = name
        self.arguments = arguments


def _refused_tool_call(exc: Exception) -> MisroutedToolCall | None:
    """The call inside a tool_use_failed error's `failed_generation`, if readable."""
    # Locate the vendor's error body; bail out quietly if it isn't the expected shape.
    body = getattr(exc, "body", None)
    if not isinstance(body, dict):
        return None
    error = body.get("error", body)
    raw = error.get("failed_generation") if isinstance(error, dict) else None
    if not raw:
        return None
    # The refused call is embedded as JSON text; parse it.
    try:
        call = json.loads(raw)
    except (TypeError, json.JSONDecodeError):
        return None
    if isinstance(call, list):
        call = call[0] if call else None
    if not isinstance(call, dict) or not isinstance(call.get("name"), str):
        return None
    # Different vendors name the argument field differently, and may send it as a string.
    arguments = call.get("arguments", call.get("parameters", {}))
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except json.JSONDecodeError:
            return None
    return MisroutedToolCall(call["name"], arguments if isinstance(arguments, dict) else {})


def _thinks_first(model: str) -> bool:
    """A model that reasons before it answers and takes `reasoning_effort` (gpt-oss)."""
    return "gpt-oss" in model


def _rate_limit_wait(exc: Exception) -> float | None:
    """Seconds to wait if `exc` is an HTTP 429, else None."""
    if getattr(exc, "status_code", None) != 429:
        return None
    # Preferred source: the standard Retry-After header (capped so we never sleep absurdly long).
    headers = getattr(getattr(exc, "response", None), "headers", None) or {}
    retry_after = headers.get("retry-after")
    if retry_after:
        try:
            return min(float(retry_after) + 0.5, 65.0)
        except ValueError:
            pass
    # Some vendors only say "try again in 1m2.5s" inside the message text.
    match = re.search(r"try again in (?:(\d+)m)?([\d.]+)s", str(exc))
    if match:
        return min(int(match.group(1) or 0) * 60 + float(match.group(2)) + 0.5, 65.0)
    # No hint at all: a modest default pause.
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
        """Recursively test one schema node against the strict-mode rules."""
        if not isinstance(node, dict):
            return True
        # Objects must be closed and fully "required".
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
        # Arrays are only as strict as their item schema.
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

    # Transient server/network conditions: retry. Client errors (4xx): don't.
    if isinstance(exc, openai.RateLimitError | openai.APITimeoutError):
        return True
    if isinstance(exc, openai.APIConnectionError):
        return True
    if isinstance(exc, openai.APIStatusError):
        return exc.status_code >= 500
    return isinstance(exc, LLMError)
