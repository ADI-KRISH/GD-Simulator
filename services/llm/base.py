"""The LLM interface the rest of the system depends on (design §54).

Nothing outside `services/llm/` imports a vendor SDK or knows a model name.
Agents ask for "structured JSON matching this schema" and get it; which model
produced it is a configuration detail (claude.md: "keep the LLM, STT, and
media-server integrations behind thin interfaces ... the architecture must stay
vendor-independent").

Two methods:

* `complete_json`      — structured output, no tools. Every evaluator agent.
* `complete_with_tools` — structured output with tool use, for the synthesis
  agent, which must not do arithmetic itself.
"""

import inspect
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable


class LLMError(RuntimeError):
    """Any failure reaching or parsing a model response.

    Callers treat this as retryable-then-degradable: an LLM failure may never
    take down a live session or lose a transcript (claude.md invariant 1).
    """


@dataclass(frozen=True, slots=True)
class ToolDefinition:
    """A function the model may call.

    The handler runs *here*, not in the model, which is the entire point when
    the tool is a scoring calculation: the number comes from code operating on
    stored data, and the model only decides which question to ask of it
    (claude.md invariant 5).
    """

    name: str
    description: str
    input_schema: dict[str, Any]
    handler: Callable[..., Any]

    async def invoke(self, arguments: dict[str, Any]) -> Any:
        """Run the handler, sync or async, and surface failure as a value.

        A tool error is returned to the model rather than raised, so it can
        correct a bad argument instead of the whole turn dying.
        """
        try:
            result = self.handler(**arguments)
            if inspect.isawaitable(result):
                result = await result
            return result
        except Exception as exc:
            return {"error": f"{type(exc).__name__}: {exc}"}


@dataclass(slots=True)
class ToolInvocation:
    """One tool call the model made. Recorded so a score's provenance is
    auditable — you can show exactly what the model asked and what it was
    told."""

    name: str
    arguments: dict[str, Any]
    result: Any


@dataclass(slots=True)
class LLMResult:
    """A parsed structured response plus what it cost."""

    data: dict[str, Any]
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    raw_text: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)
    tool_calls: list[ToolInvocation] = field(default_factory=list)


@runtime_checkable
class LLMProvider(Protocol):
    name: str
    model: str

    async def complete_json(
        self,
        *,
        system: str,
        prompt: str,
        schema: dict[str, Any],
        max_tokens: int | None = None,
    ) -> LLMResult:
        """Return JSON conforming to `schema`.

        Implementations must raise `LLMError` — never a vendor exception — so
        callers can handle failure without importing an SDK.
        """
        ...

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
        """Same, but the model may call `tools` before answering.

        Implementations run the call/execute/feed-back loop until the model
        stops asking for tools, then return JSON conforming to `schema`.
        `max_rounds` bounds it so a model that loops forever cannot hang a
        worker.
        """
        ...
