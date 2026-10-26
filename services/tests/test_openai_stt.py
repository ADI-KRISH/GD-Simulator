"""The OpenAI-compatible transcriber: Groq's Whisper on the platform's key (Day 4).

Purpose: Tests for the OpenAI-compatible speech-to-text provider (for example Groq Whisper)
    using a mocked HTTP transport, plus the setting-driven provider selection.
Why needed: Practice mode relies on this provider to turn spoken utterances into text. Mocking
    HTTP proves the request shape, segment mapping, confidence conversion and error handling
    without any network or API key.
Contains:
  - _routed: helper that sends every httpx client through a mock handler.
  - test_segments_become_utterances_with_whispers_confidence: request shape and
    segment-to-utterance mapping.
  - test_text_without_segments_is_still_heard: fallback to a single utterance from the text.
  - test_a_refusal_is_an_error_the_room_reports: HTTP errors surface as RuntimeError.
  - test_stt_provider_openai_uses_the_llm_endpoint_and_key: provider selection reuses the LLM
    endpoint and key.
Used by: nothing imports it directly (run by pytest, which discovers test_*.py files)
Depends on: services/common/config.py, services/speech/__init__.py, services/speech/stt.py
"""

import httpx
import pytest

from services.common.config import settings
from services.speech import stt


def _routed(monkeypatch, handler):
    """Route httpx clients through a mock transport."""
    # Keep the real class so the mock transport can wrap it.
    real = httpx.AsyncClient
    # Every httpx client created from now on uses the mock handler, so no network call happens.
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs),
    )


async def test_segments_become_utterances_with_whispers_confidence(monkeypatch):
    """Whisper segments become utterances with a confidence from avg_logprob."""
    # The handler stores the request it receives for later assertions.
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        """Mock Whisper endpoint that records the request."""
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.content
        # A fake verbose_json reply: two real segments and one blank one.
        return httpx.Response(
            200,
            json={
                "text": "I lean towards yes. But the cost matters.",
                "language": "en",
                "segments": [
                    {"start": 0.0, "end": 1.4, "text": " I lean towards yes.", "avg_logprob": -0.1},
                    {
                        "start": 1.4,
                        "end": 2.9,
                        "text": " But the cost matters.",
                        "avg_logprob": -0.3,
                    },
                    {"start": 2.9, "end": 3.0, "text": "  ", "avg_logprob": -2.0},
                ],
            },
        )

    # Route all HTTP through the mock handler.
    _routed(monkeypatch, handler)
    # A Groq-style base URL with a trailing slash, which must be tidied.
    provider = stt.OpenAICompatibleSTT(
        "k", "whisper-large-v3-turbo", "https://api.groq.com/openai/v1/"
    )
    # Transcribe fake audio bytes.
    heard = await provider.transcribe(b"RIFF....WAVE")
    # The request goes to /audio/transcriptions with the bearer key, the model and verbose_json.
    assert seen["url"] == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer k"
    assert b"whisper-large-v3-turbo" in seen["body"] and b"verbose_json" in seen["body"]
    # Blank segments are dropped and the text is stripped.
    assert [u.text for u in heard.utterances] == ["I lean towards yes.", "But the cost matters."]
    assert heard.text == "I lean towards yes. But the cost matters."
    # Confidence is derived from avg_logprob.
    assert 0.9 < heard.utterances[0].confidence < 0.91  # exp(-0.1)


async def test_text_without_segments_is_still_heard(monkeypatch):
    """A reply with only text still yields one utterance."""
    # A reply with text and a duration but no segments.
    _routed(monkeypatch, lambda r: httpx.Response(200, json={"text": "Yes.", "duration": 0.8}))
    heard = await stt.OpenAICompatibleSTT("k", "whisper-1", "").transcribe(b"x")
    # Wrapped as one utterance covering the whole duration.
    assert [(u.text, u.end) for u in heard.utterances] == [("Yes.", 0.8)]


async def test_a_refusal_is_an_error_the_room_reports(monkeypatch):
    """An HTTP refusal becomes a RuntimeError the room reports."""
    # A rate-limit style refusal.
    _routed(monkeypatch, lambda r: httpx.Response(429, json={"error": "rate limited"}))
    # It surfaces as a RuntimeError the room can report.
    with pytest.raises(RuntimeError, match="speech-to-text failed"):
        await stt.OpenAICompatibleSTT("k", "whisper-1", "").transcribe(b"x")


def test_stt_provider_openai_uses_the_llm_endpoint_and_key(monkeypatch):
    """The 'openai' STT provider reuses the LLM endpoint and key."""
    # Configure provider selection through settings.
    monkeypatch.setattr(settings, "stt_provider", "openai")
    monkeypatch.setattr(settings, "llm_api_key", "the-key")
    monkeypatch.setattr(settings, "openai_base_url", "https://api.groq.com/openai/v1")
    monkeypatch.setattr(settings, "openai_stt_model", "whisper-large-v3-turbo")
    # Clear the cache so selection runs again.
    stt.set_stt_provider(None)
    # Check which provider was picked and how it is configured.
    try:
        provider = stt.get_stt_provider()
        assert provider.name == "openai"
        assert provider._base == "https://api.groq.com/openai/v1"
        assert provider._model == "whisper-large-v3-turbo"
    # Always restore the stub so other tests are unaffected.
    finally:
        stt.set_stt_provider(stt.StubSTT())
