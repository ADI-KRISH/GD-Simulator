"""Text-to-speech behind a thin interface (practice mode, design §54).

ElevenLabs is the real provider; nothing outside this module knows that. The
stub produces no audio at all - it only reports how long the line would take to
say - so the rest of the practice loop (turn-taking, barge-in, tracking) runs and
is testable with no key and no network. The browser speaks stub lines itself with
`speechSynthesis`, so a fresh clone still talks.
"""

import logging
from collections.abc import AsyncIterator
from typing import Protocol, runtime_checkable

import httpx

from services.common.config import settings

logger = logging.getLogger(__name__)

# Spoken English runs at roughly this many words a second (same figure the
# text-only simulation uses).
WORDS_PER_SECOND = 2.6
# mp3_44100_128 is constant-bitrate: 128 kbit/s = 16 000 bytes/s.
MP3_BYTES_PER_SECOND = 16_000
ELEVENLABS_OUTPUT_FORMAT = "mp3_44100_128"
# Lower stability and a little style make delivery expressive instead of flat.
VOICE_SETTINGS = {
    "stability": 0.4,
    "similarity_boost": 0.8,
    "style": 0.25,
    "use_speaker_boost": True,
}


class TTSError(RuntimeError):
    """Any failure reaching the TTS vendor. Callers fall back to text-only."""


def estimate_seconds(text: str) -> float:
    return max(1.0, len(text.split()) / WORDS_PER_SECOND)


@runtime_checkable
class TTSProvider(Protocol):
    name: str
    # False = no audio bytes are produced; the client voices the text itself.
    produces_audio: bool

    def stream(self, text: str, voice_id: str) -> AsyncIterator[bytes]: ...


class StubTTS:
    name = "stub"
    produces_audio = False

    async def stream(self, text: str, voice_id: str) -> AsyncIterator[bytes]:
        return
        yield b""  # pragma: no cover - makes this an async generator


class ElevenLabsTTS:
    """Streaming speech from the ElevenLabs HTTP API.

    Chunks are forwarded as they arrive, so the first audio reaches the browser
    while the rest of the sentence is still being synthesised.
    """

    name = "elevenlabs"
    produces_audio = True

    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        self._key = api_key
        self._model = model
        self._base = base_url.rstrip("/")

    async def stream(self, text: str, voice_id: str) -> AsyncIterator[bytes]:
        url = f"{self._base}/v1/text-to-speech/{voice_id}/stream"
        try:
            async with (
                httpx.AsyncClient(timeout=30.0) as client,
                client.stream(
                    "POST",
                    url,
                    params={"output_format": ELEVENLABS_OUTPUT_FORMAT},
                    headers={"xi-api-key": self._key},
                    json={
                        "text": text,
                        "model_id": self._model,
                        "voice_settings": VOICE_SETTINGS,
                    },
                ) as response,
            ):
                if response.status_code != 200:
                    body = (await response.aread())[:200]
                    raise TTSError(f"ElevenLabs TTS returned {response.status_code}: {body!r}")
                async for chunk in response.aiter_bytes(4096):
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            raise TTSError(f"ElevenLabs TTS request failed: {exc}") from exc


_provider: TTSProvider | None = None


def get_tts_provider() -> TTSProvider:
    """ElevenLabs when a key is configured (or `TTS_PROVIDER=elevenlabs`), else the stub."""
    global _provider
    if _provider is not None:
        return _provider
    choice = (settings.tts_provider or "auto").lower()
    if choice == "elevenlabs" or (choice == "auto" and settings.elevenlabs_api_key):
        if not settings.elevenlabs_api_key:
            raise TTSError("TTS_PROVIDER=elevenlabs but ELEVENLABS_API_KEY is empty")
        _provider = ElevenLabsTTS(
            settings.elevenlabs_api_key, settings.elevenlabs_tts_model, settings.elevenlabs_base_url
        )
    else:
        logger.warning("no ElevenLabs key; practice personas will use browser speech")
        _provider = StubTTS()
    logger.info("tts provider selected", extra={"provider": _provider.name})
    return _provider


def set_tts_provider(provider: TTSProvider | None) -> None:
    """Override the provider. Used by tests."""
    global _provider
    _provider = provider
