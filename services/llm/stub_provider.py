"""Offline stub provider.

Purpose:     A fake "model" that returns deterministic, schema-valid JSON built
             from a hash of the prompt. No network, no API key.
Why needed:  Lets the whole agentic pipeline (supervisor, evaluators, aggregator,
             report) run and be unit-tested on any machine, and it is the default
             provider in a fresh checkout. Without it, every test would need a
             paid API key.
Contains:
  - `StubLLM`            implements `LLMProvider`; records every call in `.calls`
                         so tests can assert what was sent, and can be told to
                         fail so error paths are exercised.
  - `_generate`          builds a value satisfying a JSON Schema (the engine
                         behind both methods).
  - `_byte`              picks a stable pseudo-random byte from the seed so the
                         output is repeatable.
  - `_SYNTHETIC_STRINGS` visibly fake text so stub output is never mistaken for
                         a real judgement.
Used by:     llm/__init__.py (default provider), tests/conftest.py and most
             evaluation / observer / synthesis tests.
Depends on:  llm/base.py (the interface and result types).

This is not a model and does not pretend to be one. It synthesises a
deterministic, schema-valid response from a hash of the prompt so that the
agentic pipeline — supervisor, evaluators, aggregator, report — can be run and
unit-tested end to end with no API key and no network.

Why it earns its place: claude.md requires tests Claude Code actually runs, and
the parts of the evaluation pipeline that must be correct (dispatch, validation,
aggregation math, report assembly) are exactly the parts that do not depend on
what the model says. Those get tested here. Whether the *scores* are any good is
a question for a real provider, and the stub never claims otherwise — every
narrative string it emits is visibly synthetic.
"""

import hashlib  # derive deterministic output from the prompt
from typing import Any  # JSON schema / value types

from services.llm.base import LLMError, LLMResult, ToolDefinition, ToolInvocation  # interface


class StubLLM:
    """Deterministic schema-driven generator."""

    name = "stub"
    model = "stub-deterministic"

    def __init__(self, *, fail: bool = False) -> None:
        # Lets tests exercise the failure paths — retry, partial-agent failure,
        # and "evaluation degrades but the session is untouched".
        self._fail = fail
        self.calls: list[dict[str, Any]] = []

    async def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int | None = None,
    ) -> LLMResult:
        """Return a schema-valid object derived from a hash of the prompt (no model call)."""
        # Remember the call so tests can inspect exactly what an agent sent.
        self.calls.append({"system": system, "prompt": prompt, "schema": schema})
        # Simulated outage: lets tests drive the retry / degrade paths.
        if self._fail:
            raise LLMError("stub provider configured to fail")

        # Same prompt -> same seed -> same output, which keeps tests repeatable.
        seed = hashlib.sha256(prompt.encode("utf-8")).digest()
        return LLMResult(
            data=_generate(schema, seed, 0),
            model=self.model,
            input_tokens=len(prompt) // 4,
            output_tokens=64,
            raw_text="",
            metadata={"synthetic": True},
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
        """Calls every tool once, then answers.

        A real model decides *whether* to use a tool; the stub always does, on
        purpose. That way the offline suite exercises the whole loop — argument
        construction, handler execution, error handling, invocation recording —
        rather than skipping the path that only runs when a real model is
        connected.
        """
        # Record the call (including which tools were offered) for test assertions.
        self.calls.append(
            {"system": system, "prompt": prompt, "schema": schema, "tools": [t.name for t in tools]}
        )
        if self._fail:
            raise LLMError("stub provider configured to fail")

        seed = hashlib.sha256(prompt.encode("utf-8")).digest()
        invocations: list[ToolInvocation] = []
        # Call each offered tool once with schema-valid arguments, so the real
        # handler code (e.g. the score calculators) runs in tests.
        for index, tool in enumerate(tools):
            arguments = _generate(tool.input_schema, seed, index)
            # A tool with no usable object schema is called with no arguments.
            if not isinstance(arguments, dict):
                arguments = {}
            invocations.append(
                ToolInvocation(
                    name=tool.name, arguments=arguments, result=await tool.invoke(arguments)
                )
            )

        # After the tool round, produce the final structured answer.
        return LLMResult(
            data=_generate(schema, seed, 0),
            model=self.model,
            input_tokens=len(prompt) // 4,
            output_tokens=64,
            raw_text="",
            metadata={"synthetic": True, "rounds": 2 if tools else 1},
            tool_calls=invocations,
        )


# Visibly fake text so stub output is never mistaken for a real judgement.
_SYNTHETIC_STRINGS = (
    "[stub] observation generated without a model",
    "[stub] placeholder evidence, not a real judgement",
    "[stub] synthetic narrative for pipeline testing",
)


def _byte(seed: bytes, depth: int, salt: int = 0) -> int:
    """Pick a stable pseudo-random byte (0-255) from the seed for this position."""
    return seed[(depth * 7 + salt) % len(seed)]


def _generate(
    schema: dict[str, Any], seed: bytes, depth: int, item_index: int | None = None
) -> Any:
    """Build a value satisfying `schema`.

    Handles the subset of JSON Schema the contracts in this project use:
    objects with `required`/`properties`, arrays with `minItems`/`maxItems`,
    strings, numbers with `minimum`/`maximum`, integers, booleans and enums.

    `item_index` is threaded down from an array so that enum-valued fields
    differ between sibling items. Without it a batched evaluation — one array
    entry per candidate, `candidate_id` an enum of the labels — would hand the
    same candidate back several times and fail its uniqueness check for reasons
    that have nothing to do with the code under test.
    """
    # Enum: choose one allowed value (rotating by array position when inside a list).
    if "enum" in schema:
        options = schema["enum"]
        if item_index is not None:
            return options[item_index % len(options)]
        return options[_byte(seed, depth) % len(options)]

    kind = schema.get("type", "object")

    # Object: fill each required property recursively.
    if kind == "object":
        properties: dict[str, Any] = schema.get("properties", {})
        required = schema.get("required", list(properties))
        return {
            key: _generate(properties[key], seed, depth + index + 1, item_index)
            for index, key in enumerate(required)
            if key in properties
        }

    # Array: decide how many items, then generate each one.
    if kind == "array":
        item_schema = schema.get("items", {"type": "string"})
        # Honour an exact-length array (a batched response is one entry per
        # candidate); otherwise pick a small arbitrary count.
        minimum = schema.get("minItems")
        maximum = schema.get("maxItems")
        if minimum is not None:
            count = minimum
        elif maximum is not None:
            count = min(maximum, 1 + _byte(seed, depth) % 2)
        else:
            count = 1 + _byte(seed, depth) % 2
        return [_generate(item_schema, seed, depth + i + 3, i) for i in range(count)]

    # Number / integer: scale a seed byte into the allowed [minimum, maximum] range.
    if kind in {"number", "integer"}:
        low = schema.get("minimum", 0)
        high = schema.get("maximum", 10 if kind == "number" else 5)
        span = max(high - low, 0)
        value = low + (span * (_byte(seed, depth) / 255))
        return int(round(value)) if kind == "integer" else round(value, 2)

    # Boolean: a coin flip from the seed.
    if kind == "boolean":
        return bool(_byte(seed, depth) % 2)

    # Strings. A field named like a timestamp gets a timestamp, because the
    # evidence contract expects "mm:ss" and a random word there would be a
    # confusing failure rather than an obvious stub.
    if "timestamp" in str(schema.get("description", "")).lower():
        total = _byte(seed, depth) * 3
        return f"{total // 60:02d}:{total % 60:02d}"
    return _SYNTHETIC_STRINGS[_byte(seed, depth) % len(_SYNTHETIC_STRINGS)]
