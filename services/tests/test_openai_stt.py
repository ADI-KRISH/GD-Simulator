"""The OpenAI-compatible transcriber: Groq's Whisper on the platform's key (Day 4)."""

import httpx
import pytest

from services.common.config import settings
from services.speech import stt


def _routed(monkeypatch, handler):
    real = httpx.AsyncClient
    monkeypatch.setattr(
        httpx,
        "AsyncClient",
        lambda **kwargs: real(transport=httpx.MockTransport(handler), **kwargs),
    )


async def test_segments_become_utterances_with_whispers_confidence(monkeypatch):
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["url"] = str(request.url)
        seen["auth"] = request.headers["authorization"]
        seen["body"] = request.content
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

    _routed(monkeypatch, handler)
    provider = stt.OpenAICompatibleSTT(
        "k", "whisper-large-v3-turbo", "https://api.groq.com/openai/v1/"
    )
    heard = await provider.transcribe(b"RIFF....WAVE")
    assert seen["url"] == "https://api.groq.com/openai/v1/audio/transcriptions"
    assert seen["auth"] == "Bearer k"
    assert b"whisper-large-v3-turbo" in seen["body"] and b"verbose_json" in seen["body"]
    assert [u.text for u in heard.utterances] == ["I lean towards yes.", "But the cost matters."]
    assert heard.text == "I lean towards yes. But the cost matters."
    assert 0.9 < heard.utterances[0].confidence < 0.91  # exp(-0.1)


async def test_text_without_segments_is_still_heard(monkeypatch):
    _routed(monkeypatch, lambda r: httpx.Response(200, json={"text": "Yes.", "duration": 0.8}))
    heard = await stt.OpenAICompatibleSTT("k", "whisper-1", "").transcribe(b"x")
    assert [(u.text, u.end) for u in heard.utterances] == [("Yes.", 0.8)]


async def test_a_refusal_is_an_error_the_room_reports(monkeypatch):
    _routed(monkeypatch, lambda r: httpx.Response(429, json={"error": "rate limited"}))
    with pytest.raises(RuntimeError, match="speech-to-text failed"):
        await stt.OpenAICompatibleSTT("k", "whisper-1", "").transcribe(b"x")


def test_stt_provider_openai_uses_the_llm_endpoint_and_key(monkeypatch):
    monkeypatch.setattr(settings, "stt_provider", "openai")
    monkeypatch.setattr(settings, "llm_api_key", "the-key")
    monkeypatch.setattr(settings, "openai_base_url", "https://api.groq.com/openai/v1")
    monkeypatch.setattr(settings, "openai_stt_model", "whisper-large-v3-turbo")
    stt.set_stt_provider(None)
    try:
        provider = stt.get_stt_provider()
        assert provider.name == "openai"
        assert provider._base == "https://api.groq.com/openai/v1"
        assert provider._model == "whisper-large-v3-turbo"
    finally:
        stt.set_stt_provider(stt.StubSTT())
