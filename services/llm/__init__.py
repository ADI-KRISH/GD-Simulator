"""LLM layer: one interface, swappable providers (design §54)."""

import logging

from services.common.config import settings
from services.llm.base import LLMError, LLMProvider, LLMResult
from services.llm.stub_provider import StubLLM

logger = logging.getLogger(__name__)

__all__ = [
    "LLMError",
    "LLMProvider",
    "LLMResult",
    "StubLLM",
    "get_llm_provider",
    "set_llm_provider",
]

_provider: LLMProvider | None = None


def get_llm_provider() -> LLMProvider:
    """Process-wide provider chosen from settings.

    Defaults to the stub so a fresh clone runs the whole pipeline offline. Set
    `LLM_PROVIDER` to `anthropic` or `openai` (plus `LLM_API_KEY` and a matching
    `LLM_MODEL`) for real evaluation.
    """
    global _provider
    if _provider is None:
        choice = settings.llm_provider.lower()
        if choice == "anthropic":
            from services.llm.anthropic_provider import AnthropicProvider

            _provider = AnthropicProvider()
        elif choice == "openai":
            from services.llm.openai_provider import OpenAIProvider

            _provider = OpenAIProvider()
        else:
            _provider = StubLLM()
        logger.info(
            "llm provider selected",
            extra={"provider": _provider.name, "model": _provider.model},
        )
    return _provider


def set_llm_provider(provider: LLMProvider | None) -> None:
    """Override the provider. Used by tests and by the evaluation worker."""
    global _provider
    _provider = provider
