"""Text-to-speech behind a thin interface (practice mode, design §54).

ElevenLabs is the real provider; nothing outside this module knows that. The
stub produces no audio at all - it only reports how long the line would take to
say - so the rest of the practice loop (turn-taking, barge-in, tracking) runs and
is testable with no key and no network. The browser speaks stub lines itself with
`speechSynthesis`, so a fresh clone still talks.

Purpose: Text-to-speech behind one interface (TTSProvider) with three implementations:
    ElevenLabs streaming, free Microsoft Edge voices, and a no-audio stub that lets the browser
    speak the text itself.
Why needed: Practice mode needs AI personas that talk. Hiding the vendor behind TTSProvider
    means the turn-taking loop works with any voice backend, or with none, and a TTS failure
    only drops a persona back to text.
Contains:
  - Constants (WORDS_PER_SECOND, MP3_BYTES_PER_SECOND, VOICE_SETTINGS): used to estimate
    speaking time and tune delivery.
  - TTSError: raised on any vendor failure; callers fall back to text-only.
  - estimate_seconds: rough speaking time of a line from its word count.
  - TTSProvider: protocol with a streaming method and byte-rate metadata.
  - StubTTS: yields no audio; the browser uses speechSynthesis.
  - ElevenLabsTTS: streams mp3 chunks over HTTP.
  - EdgeVoice / edge_voice_for / EdgeTTS: map each persona's stored voice to an Edge neural
    voice with its own pace and pitch.
  - get_tts_provider / set_tts_provider: pick the process-wide provider from settings, or
    override it in tests.
Used by: services/practice/engine.py, services/practice/router.py, services/simulation.py,
    services/tests/test_edge_voices.py, services/tests/test_panel.py,
    services/tests/test_practice.py
Depends on: services/common/config.py
"""

import logging  # provider selection logs
from collections.abc import AsyncIterator  # TTS streams audio bytes as they arrive
from dataclasses import dataclass  # EdgeVoice container
from typing import Protocol, runtime_checkable  # the TTSProvider interface

import httpx  # HTTP streaming to the ElevenLabs API

from services.common.config import settings  # provider choice, key, model, base URL

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
    """Rough speaking time for a line (at least one second), from its word count."""
    # Never less than one second, so very short lines still get a turn-taking window.
    return max(1.0, len(text.split()) / WORDS_PER_SECOND)


@runtime_checkable
class TTSProvider(Protocol):
    """What the practice loop needs from any text-to-speech backend."""

    name: str
    # Used to turn streamed byte counts into a duration for turn-taking.
    mp3_bytes_per_second: int
    # False = no audio bytes are produced; the client voices the text itself.
    produces_audio: bool

    def stream(self, text: str, voice_id: str) -> AsyncIterator[bytes]:
        """Yield mp3 bytes for `text` in the given voice."""
        ...


class StubTTS:
    """No-audio provider: the browser speaks the text itself with speechSynthesis."""

    name = "stub"
    mp3_bytes_per_second = MP3_BYTES_PER_SECOND
    produces_audio = False

    async def stream(self, text: str, voice_id: str) -> AsyncIterator[bytes]:
        """Yield nothing (an empty async generator)."""
        return
        yield b""  # pragma: no cover - makes this an async generator


class ElevenLabsTTS:
    """Streaming speech from the ElevenLabs HTTP API.

    Chunks are forwarded as they arrive, so the first audio reaches the browser
    while the rest of the sentence is still being synthesised.
    """

    name = "elevenlabs"
    produces_audio = True
    mp3_bytes_per_second = MP3_BYTES_PER_SECOND

    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        self._key = api_key
        self._model = model
        self._base = base_url.rstrip("/")

    async def stream(self, text: str, voice_id: str) -> AsyncIterator[bytes]:
        """Stream mp3 chunks from ElevenLabs; raises TTSError on any HTTP failure."""
        # The streaming endpoint for the chosen voice.
        url = f"{self._base}/v1/text-to-speech/{voice_id}/stream"
        # Network and HTTP errors are converted to TTSError so callers can fall back to text-only.
        try:
            # Open the HTTP client and the streaming POST together so both close on exit.
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
                # Non-200 means no audio: raise with a snippet of the body for diagnosis.
                if response.status_code != 200:
                    body = (await response.aread())[:200]
                    raise TTSError(f"ElevenLabs TTS returned {response.status_code}: {body!r}")
                # Forward bytes as they arrive so playback can start before synthesis finishes.
                async for chunk in response.aiter_bytes(4096):
                    if chunk:
                        yield chunk
        except httpx.HTTPError as exc:
            raise TTSError(f"ElevenLabs TTS request failed: {exc}") from exc


@dataclass(frozen=True, slots=True)
class EdgeVoice:
    """An Edge neural voice plus how this persona delivers it.

    Rate and pitch are what make two people on the same voice sound like two people,
    and a quiet thinker sound unlike a fast talker.
    """

    voice: str
    rate: str = "+0%"
    pitch: str = "+0Hz"


# Preset ElevenLabs voice id -> Edge voice and delivery. Indian-English voices where
# Edge has them (it has three), then the most natural neural voices with a nearby
# accent, so a six-person panel has six distinguishable speakers.
_EDGE_BY_PRESET_VOICE: dict[str, EdgeVoice] = {
    # arjun, the analyst: measured, clear
    "cjVigY5qzO86Huf0OWal": EdgeVoice("en-IN-PrabhatNeural", "-4%", "+0Hz"),
    # bhavana, the devil's advocate: quick, sharper
    "XrExE9yKIg1WjnnlVkGX": EdgeVoice("en-IN-NeerjaNeural", "+6%", "+3Hz"),
    # chitra, the diplomat: calm, warm, unhurried
    "EXAVITQu4vr4xnSDxMaL": EdgeVoice("en-IN-NeerjaExpressiveNeural", "-8%", "-3Hz"),
    # dev, the dominator: fast, confident
    "IKne3meq5aSn9XLyUdCD": EdgeVoice("en-US-AndrewMultilingualNeural", "+12%", "-2Hz"),
    # esha, the quiet thinker: soft, slow
    "pFZP5JQG7iQjIQuC4Bku": EdgeVoice("en-SG-LunaNeural", "-10%", "-2Hz"),
    # farhan, the industry expert: warm storyteller
    "JBFqnCBsd6RMkjVDRZzb": EdgeVoice("en-GB-RyanNeural", "-2%", "-4Hz"),
}
_EDGE_DEFAULT_VOICE = EdgeVoice("en-IN-NeerjaNeural")


def edge_voice_for(voice_id: str) -> EdgeVoice:
    """A persona's stored voice_id -> Edge voice. A bare Edge name is used as is."""
    # A bare Edge voice name is used as is.
    if "Neural" in voice_id:
        return EdgeVoice(voice_id)
    # Otherwise map the ElevenLabs preset id, falling back to a default voice.
    return _EDGE_BY_PRESET_VOICE.get(voice_id, _EDGE_DEFAULT_VOICE)


class EdgeTTS:
    """Free neural voices from Microsoft Edge's read-aloud service. No key.

    Needs internet but no account. The persona's stored voice_id is an ElevenLabs
    id, so it is mapped to an Edge voice with its own pace and pitch; a voice_id that
    is already an Edge voice name (e.g. "en-IN-PrabhatNeural") is used as is.
    """

    name = "edge"
    produces_audio = True
    # Edge streams 24 kHz / 48 kbit/s mp3 = 6 000 bytes/s.
    mp3_bytes_per_second = 6_000

    async def stream(self, text: str, voice_id: str) -> AsyncIterator[bytes]:
        """Stream mp3 chunks from the Edge read-aloud service in the mapped voice."""
        # edge-tts is optional, so import it only when this provider is used.
        try:
            import edge_tts  # optional dependency, imported only when this provider is used
        except ImportError as exc:
            raise TTSError("edge-tts is not installed (pip install edge-tts)") from exc
        # Per-persona voice, pace and pitch.
        choice = edge_voice_for(voice_id)
        # Stream the audio chunks and skip the metadata chunks Edge interleaves.
        try:
            async for chunk in edge_tts.Communicate(
                text, choice.voice, rate=choice.rate, pitch=choice.pitch
            ).stream():
                if chunk["type"] == "audio" and chunk["data"]:
                    yield chunk["data"]
        except Exception as exc:  # the library raises its own and aiohttp errors
            raise TTSError(f"Edge TTS failed: {exc}") from exc


# Process-wide provider, created on first use by get_tts_provider().
_provider: TTSProvider | None = None


def get_tts_provider() -> TTSProvider:
    """ElevenLabs when a key is configured (or `TTS_PROVIDER=elevenlabs`), else the stub."""
    global _provider
    # Cached after first use.
    if _provider is not None:
        return _provider
    # auto prefers ElevenLabs when a key exists, otherwise the stub.
    choice = (settings.tts_provider or "auto").lower()
    # Edge: free neural voices, no key.
    if choice == "edge":
        _provider = EdgeTTS()
    # ElevenLabs by request, or because auto found a key.
    elif choice == "elevenlabs" or (choice == "auto" and settings.elevenlabs_api_key):
        # Explicitly asked for ElevenLabs without a key: fail loudly rather than go silent.
        if not settings.elevenlabs_api_key:
            raise TTSError("TTS_PROVIDER=elevenlabs but ELEVENLABS_API_KEY is empty")
        _provider = ElevenLabsTTS(
            settings.elevenlabs_api_key, settings.elevenlabs_tts_model, settings.elevenlabs_base_url
        )
    # No key: practice personas are spoken by the browser's built-in voices.
    else:
        logger.warning("no ElevenLabs key; practice personas will use browser speech")
        _provider = StubTTS()
    logger.info("tts provider selected", extra={"provider": _provider.name})
    return _provider


def set_tts_provider(provider: TTSProvider | None) -> None:
    """Override the provider. Used by tests."""
    global _provider
    # None clears the cache so the next call re-selects.
    _provider = provider
