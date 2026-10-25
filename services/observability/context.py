"""Who is making a call: tags that ride along with a span without being passed around.

Purpose:     A `contextvars` dict (session, run, purpose, agent, dimension, candidate)
             that any code can set for the calls made inside a `with` block.
Why needed:  The model call happens deep inside a provider that knows nothing about
             sessions or agents. Setting the tags once, where the caller knows them,
             lets the tracer label the call without changing a single signature.
             Asyncio tasks copy the context, so LangGraph's parallel branches inherit it.
Used by:     observability/recorder.py (reads), evaluation/, practice/, observer/ (set).
"""

from collections.abc import Iterator
from contextlib import contextmanager
from contextvars import ContextVar

_tags: ContextVar[dict | None] = ContextVar("trace_tags", default=None)


def current() -> dict:
    """The tags in force right now (a copy)."""
    return dict(_tags.get() or {})


@contextmanager
def trace_context(**tags: str | None) -> Iterator[None]:
    """Add `tags` for everything run inside the block; None values are ignored."""
    token = _tags.set({**(_tags.get() or {}), **{k: v for k, v in tags.items() if v is not None}})
    try:
        yield
    finally:
        _tags.reset(token)
