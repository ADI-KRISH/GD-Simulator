"""The LLM layer's vendor independence (design §54, claude.md).

The claim these defend is that swapping model vendors touches one file. So they
check the *interface* — provider selection, the strict-mode subset detection
that decides how a schema is sent to OpenAI, and that failures surface as
`LLMError` rather than a vendor exception leaking upward.

No network calls: the adapters' transport is exercised by running the app, not
by mocking an HTTP client into a shape that proves nothing.
"""

import pytest

from services.common.config import settings
from services.common.contracts import load_schema
from services.evaluation.agents.definitions import CONTENT
from services.llm import get_llm_provider, set_llm_provider
from services.llm.base import LLMError, LLMProvider, LLMResult
from services.llm.openai_provider import OpenAIProvider, _strict_compatible
from services.llm.stub_provider import StubLLM
from services.observer.agent import _observation_schema


@pytest.fixture(autouse=True)
def restore_provider():
    """Provider selection is process-wide; put it back afterwards."""
    original_provider = settings.llm_provider
    original_model = settings.llm_model
    yield
    settings.llm_provider = original_provider
    settings.llm_model = original_model
    set_llm_provider(None)


@pytest.mark.parametrize(
    ("configured", "expected"),
    [("stub", "stub"), ("openai", "openai"), ("anthropic", "anthropic"), ("nonsense", "stub")],
)
def test_provider_selection_follows_settings(configured, expected):
    """An unknown value falls back to the stub rather than failing to boot."""
    settings.llm_provider = configured
    set_llm_provider(None)
    assert get_llm_provider().name == expected


def test_every_provider_satisfies_the_interface():
    """The whole contract the rest of the system depends on."""
    settings.llm_provider = "openai"
    set_llm_provider(None)
    assert isinstance(get_llm_provider(), LLMProvider)

    settings.llm_provider = "stub"
    set_llm_provider(None)
    assert isinstance(get_llm_provider(), LLMProvider)


def test_openai_takes_its_model_from_settings():
    settings.llm_model = "gpt-4o-mini"
    assert OpenAIProvider().model == "gpt-4o-mini"
    # An explicit argument still wins, so a worker can pin a different model.
    assert OpenAIProvider(model="gpt-4o").model == "gpt-4o"


# --------------------------------------------------------------------------
# Strict-mode subset detection
#
# Claiming strict for a schema that does not qualify is a 400 from the API, so
# getting this wrong fails every evaluation at runtime rather than in review.
# --------------------------------------------------------------------------


def test_the_evaluation_output_contract_qualifies_for_strict_mode():
    """Every property required, additionalProperties false throughout."""
    schema = CONTENT.response_schema("A")
    assert _strict_compatible(schema) is True


def test_the_rolling_state_contract_does_not_qualify():
    """It has an optional field and a string-keyed map — both outside the
    strict subset. It must be sent non-strict, not rejected."""
    assert _strict_compatible(_observation_schema()) is False


def test_an_optional_property_disqualifies_a_schema():
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["a"],
        "properties": {"a": {"type": "string"}, "b": {"type": "string"}},
    }
    assert _strict_compatible(schema) is False


def test_an_open_object_disqualifies_a_schema():
    assert (
        _strict_compatible(
            {"type": "object", "required": ["a"], "properties": {"a": {"type": "string"}}}
        )
        is False
    )


def test_a_string_map_disqualifies_a_schema():
    """`additionalProperties: {schema}` is a map, which strict mode rejects."""
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["positions"],
        "properties": {
            "positions": {"type": "object", "additionalProperties": {"type": "string"}}
        },
    }
    assert _strict_compatible(schema) is False


def test_nested_objects_are_checked():
    """The evidence array's item schema is nested two levels down."""
    bad = {
        "type": "object",
        "additionalProperties": False,
        "required": ["items"],
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    # Missing additionalProperties: false.
                    "required": ["x"],
                    "properties": {"x": {"type": "string"}},
                },
            }
        },
    }
    assert _strict_compatible(bad) is False


def test_the_raw_contract_files_are_unchanged_by_the_check():
    """The detection must not mutate the cached schema it inspects."""
    before = load_schema("evaluation_output")
    snapshot = str(before)
    _strict_compatible(before)
    assert str(load_schema("evaluation_output")) == snapshot


# --------------------------------------------------------------------------
# Failure surfaces
# --------------------------------------------------------------------------


async def test_a_provider_failure_is_an_llm_error_not_a_vendor_exception():
    """Callers handle failure without importing a vendor's exception types."""
    provider = StubLLM(fail=True)
    with pytest.raises(LLMError):
        await provider.complete_json(system="s", prompt="p", schema={"type": "object"})


async def test_the_stub_returns_a_schema_shaped_result():
    provider = StubLLM()
    result = await provider.complete_json(
        system="s", prompt="p", schema=CONTENT.response_schema("A")
    )
    assert isinstance(result, LLMResult)
    assert set(result.data) >= {"candidate_id", "dimension", "score", "confidence"}
    assert result.metadata["synthetic"] is True


async def test_the_stub_is_deterministic():
    """Same prompt, same output — the property that makes the pipeline testable."""
    provider = StubLLM()
    schema = CONTENT.response_schema("A")
    first = await provider.complete_json(system="s", prompt="identical", schema=schema)
    second = await provider.complete_json(system="s", prompt="identical", schema=schema)
    assert first.data == second.data


async def test_agents_are_pinned_to_one_candidate_and_dimension():
    """A single-value enum is what stops an agent scoring the wrong person —
    it holds for whichever provider is behind the interface."""
    schema = CONTENT.response_schema("C")
    assert schema["properties"]["candidate_id"]["enum"] == ["C"]
    assert schema["properties"]["dimension"]["enum"] == ["content"]
