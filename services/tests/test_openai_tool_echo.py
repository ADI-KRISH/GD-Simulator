"""Tool calls are echoed back to the model with vendor extras intact."""

from types import SimpleNamespace

from services.llm.openai_provider import _echo_tool_call


def _call(extra):
    return SimpleNamespace(
        id="call_1",
        function=SimpleNamespace(name="compute_final_score", arguments="{}"),
        model_extra=extra,
    )


def test_gemini_thought_signature_is_echoed_back():
    extra = {"extra_content": {"google": {"thought_signature": "abc"}}}
    echoed = _echo_tool_call(_call(extra))
    assert echoed["extra_content"] == extra["extra_content"]
    assert echoed["function"] == {"name": "compute_final_score", "arguments": "{}"}


def test_plain_openai_calls_are_unchanged():
    echoed = _echo_tool_call(_call(None))
    assert "extra_content" not in echoed
    assert echoed["id"] == "call_1" and echoed["type"] == "function"
