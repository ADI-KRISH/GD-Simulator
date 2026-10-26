"""LLM layer: one interface, swappable providers (design §54).

Purpose:     The public entry point of the LLM package. Picks which model backend
             to use from settings and hands out a shared provider instance.
Why needed:  Every agent asks `get_llm_provider()` instead of building a vendor
             client, so switching Anthropic / OpenAI / offline stub is a `.env`
             change with no code edits.
Contains:
  - `get_llm_provider`          the main model (evaluation, observer); defaults
                                to the offline stub so a fresh clone just runs.
  - `get_persona_llm_provider`  a smaller, faster model for practice personas,
                                because spoken-line latency is what students feel.
  - `set_llm_provider`          override hook for tests and the evaluation worker.
Used by:     evaluation/agents/base.py, evaluation/synthesis.py,
             evaluation/group_synthesis.py, observer/agent.py, practice/engine.py,
             simulation.py and many tests (conftest.py swaps in the stub).
Depends on:  common/config.py (settings), llm/base.py (the interface),
             llm/stub_provider.py; the Anthropic and OpenAI providers are
             imported lazily so an unused vendor SDK is never required.
"""

import logging  # provider selection logs

from services.common.config import settings  # llm_provider, model names
from services.llm.base import LLMError, LLMProvider, LLMResult  # the shared interface
from services.llm.stub_provider import StubLLM  # offline default provider

logger = logging.getLogger(__name__)

# Public surface: callers import from `services.llm`, never from a vendor module.
__all__ = [
    "LLMError",
    "LLMProvider",
    "LLMResult",
    "StubLLM",
    "get_llm_provider",
    "get_persona_llm_provider",
    "get_provider_for_model",
    "set_llm_provider",
]

# Process-wide providers, created lazily on first use.
_provider: LLMProvider | None = None


def get_llm_provider() -> LLMProvider:
    """Process-wide provider chosen from settings.

    Defaults to the stub so a fresh clone runs the whole pipeline offline. Set
    `LLM_PROVIDER` to `anthropic` or `openai` (plus `LLM_API_KEY` and a matching
    `LLM_MODEL`) for real evaluation.
    """
    global _provider
    # Build the provider once and reuse it for the life of the process.
    if _provider is None:
        choice = settings.llm_provider.lower()
        # Vendor modules are imported lazily so an unused SDK is never required.
        if choice == "anthropic":
            from services.llm.anthropic_provider import AnthropicProvider

            _provider = AnthropicProvider()
        elif choice == "openai":
            from services.llm.openai_provider import OpenAIProvider

            _provider = OpenAIProvider()
        else:
            # Anything else (including "stub") means the offline deterministic model.
            _provider = StubLLM()
        # Every call through the provider is recorded (observability/).
        if settings.trace_enabled:
            from services.observability.tracing import TracingProvider

            _provider = TracingProvider(_provider)
        # Log the choice once so it is obvious which model a run used.
        logger.info(
            "llm provider selected",
            extra={"provider": _provider.name, "model": _provider.model},
        )
    return _provider


_persona_provider: LLMProvider | None = None


def get_persona_llm_provider() -> LLMProvider:
    """The model that voices the practice personas.

    A small fast model is plenty for a one-sentence spoken line, and latency is
    what the student feels. Falls back to the main provider when no persona model
    is configured, or when the main provider is not a real vendor (stub, tests).
    """
    global _persona_provider
    base = get_llm_provider()
    model = settings.persona_llm_model.strip()
    # No separate persona model configured (or stub/test provider): reuse the main one.
    if not model or base.name not in {"openai", "anthropic"} or base.model == model:
        return base
    # (Re)build the persona provider only when it is missing or the model changed.
    if _persona_provider is None or _persona_provider.model != model:
        if base.name == "openai":
            from services.llm.openai_provider import OpenAIProvider

            _persona_provider = OpenAIProvider(model=model)
        else:
            from services.llm.anthropic_provider import AnthropicProvider

            _persona_provider = AnthropicProvider(model=model)
        if settings.trace_enabled:
            from services.observability.tracing import TracingProvider

            _persona_provider = TracingProvider(_persona_provider)
    return _persona_provider


def get_provider_for_model(model: str) -> LLMProvider:
    """The main provider's vendor with a different model, for experiments.

    The stub (and any provider that is not a real vendor) cannot change model, so
    it is returned as is.
    """
    base = get_llm_provider()
    model = model.strip()
    if not model or base.model == model or base.name not in {"openai", "anthropic"}:
        return base
    if base.name == "openai":
        from services.llm.openai_provider import OpenAIProvider

        provider: LLMProvider = OpenAIProvider(model=model)
    else:
        from services.llm.anthropic_provider import AnthropicProvider

        provider = AnthropicProvider(model=model)
    if settings.trace_enabled:
        from services.observability.tracing import TracingProvider

        provider = TracingProvider(provider)
    return provider


def set_llm_provider(provider: LLMProvider | None) -> None:
    """Override the provider. Used by tests and by the evaluation worker."""
    global _provider
    # Replacing (or clearing with None) forces the next call to use this instance.
    _provider = provider
