"""Wrap an LLM provider so every call is recorded.

Purpose:     `TracingProvider` has the same interface as any provider, forwards each
             call, and records prompt, output, tokens, latency, tool calls and errors.
Why needed:  `get_llm_provider()` is the one door every agent walks through, so
             wrapping it there traces the evaluators, synthesis, observer and the
             practice personas without touching any of them.
"""

import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

from services.llm.base import LLMProvider, LLMResult, ToolDefinition
from services.observability.context import current
from services.observability.recorder import record

_PREVIEW = 4000  # characters of one tool result kept in the trace


class TracingProvider:
    """Transparent decorator over an `LLMProvider`."""

    def __init__(self, inner: LLMProvider) -> None:
        self._inner = inner
        self.name = inner.name
        self.model = inner.model

    def __getattr__(self, item: str) -> Any:
        """Anything else (private helpers, settings) comes from the real provider."""
        return getattr(self._inner, item)

    async def complete_json(
        self, *, system: str, prompt: str, schema: dict[str, Any], max_tokens: int | None = None
    ) -> LLMResult:
        """Forward the call and record it."""
        return await self._traced(
            system,
            prompt,
            schema,
            max_tokens,
            lambda: self._inner.complete_json(
                system=system, prompt=prompt, schema=schema, max_tokens=max_tokens
            ),
        )

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
        """Forward the tool-using call and record it with the tool calls it made."""
        return await self._traced(
            system,
            prompt,
            schema,
            max_tokens,
            lambda: self._inner.complete_with_tools(
                system=system,
                prompt=prompt,
                tools=tools,
                schema=schema,
                max_tokens=max_tokens,
                max_rounds=max_rounds,
            ),
            tools=[t.name for t in tools],
        )

    async def _traced(
        self,
        system: str,
        prompt: str,
        schema: dict,
        max_tokens: int | None,
        call: Callable[[], Awaitable[LLMResult]],
        tools: list[str] | None = None,
    ) -> LLMResult:
        """Run `call`, time it, record success or failure, and return/raise as normal."""
        tags = current()
        meta = {
            "max_tokens": max_tokens,
            "schema_fields": list(schema.get("properties", {})),
            "tools": tools or [],
        }
        base = {
            "kind": "llm",
            "started_at": datetime.now(UTC),
            "purpose": tags.get("purpose", "unknown"),
            "name": tags.get("agent") or tags.get("purpose", "llm"),
            "provider": self.name,
            "model": self.model,
            "system_prompt": system,
            "prompt": prompt,
        }
        t0 = time.perf_counter()
        try:
            result = await call()
        except Exception as exc:
            record(
                **base,
                meta=meta,
                latency_ms=(time.perf_counter() - t0) * 1000,
                status="error",
                error=f"{type(exc).__name__}: {exc}"[:2000],
            )
            raise
        record(
            **{**base, "model": result.model or self.model},
            meta={**meta, **(result.metadata or {})},
            latency_ms=(time.perf_counter() - t0) * 1000,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            output=result.data,
            tool_calls=[
                {"name": t.name, "arguments": t.arguments, "result": str(t.result)[:_PREVIEW]}
                for t in result.tool_calls
            ],
        )
        return result
