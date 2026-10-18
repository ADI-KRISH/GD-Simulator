"""Speech-to-text behind a thin interface (FR-13, design §11, §54).

Whisper is the local implementation, but nothing outside this module knows
that: callers depend on `STTProvider`. Swapping in a managed ASR provider means
adding one class here (claude.md "keep the LLM, STT, and media-server
integrations behind thin interfaces").

Providers:

* `WhisperSTT`   — faster-whisper, running locally on CPU. The real path.
* `ElevenLabsSTT`, `OpenAICompatibleSTT` — managed ASR over HTTP, one
                   utterance at a time (practice mode).
* `StubSTT`      — deterministic, no model download, no network. Used by the
                   test suite and by anyone who wants to exercise the pipeline
                   end to end before pulling ~1 GB of model weights.

The stub is selected automatically when the Whisper runtime is not installed,
so a fresh clone can run the whole pipeline immediately.
"""

import asyncio
import hashlib
import logging
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from services.common.config import settings

logger = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class Utterance:
    """One recognised span within a chunk, relative to the chunk start."""

    start: float
    end: float
    text: str
    confidence: float


@dataclass(frozen=True, slots=True)
class Transcription:
    utterances: list[Utterance] = field(default_factory=list)
    language: str = "en"

    @property
    def text(self) -> str:
        return " ".join(u.text for u in self.utterances).strip()


@runtime_checkable
class STTProvider(Protocol):
    """The whole contract the rest of the system depends on."""

    name: str

    async def transcribe(self, audio: bytes, *, language: str | None = None) -> Transcription: ...


class StubSTT:
    """Deterministic stand-in for Whisper.

    Derives a stable pseudo-transcript from a hash of the audio bytes. It is not
    pretending to be speech recognition — it exists so the chunking, storage,
    metrics, observer and evaluation stages can be developed and tested without
    a model, and so the same input always produces the same output.
    """

    name = "stub"

    # Recognisable filler so a stub transcript is never mistaken for real text.
    _PHRASES = (
        "I think the key consideration here is the trade-off involved.",
        "Building on that point, there is evidence pointing the other way.",
        "Could we look at who actually bears the cost of that?",
        "I disagree, and here is the reasoning behind it.",
        "To summarise where we have got to so far.",
        "That is a fair point, and it strengthens the earlier argument.",
    )

    def __init__(self, chunk_seconds: float = 30.0) -> None:
        self._chunk_seconds = chunk_seconds

    async def transcribe(self, audio: bytes, *, language: str | None = None) -> Transcription:
        digest = hashlib.sha256(audio).digest()
        count = 1 + digest[0] % 3
        utterances = []
        span = self._chunk_seconds / count
        for i in range(count):
            phrase = self._PHRASES[digest[i + 1] % len(self._PHRASES)]
            utterances.append(
                Utterance(
                    start=i * span,
                    end=(i + 1) * span,
                    text=phrase,
                    # 0.80–0.99, stable for a given input.
                    confidence=0.80 + (digest[i + 4] % 20) / 100,
                )
            )
        return Transcription(utterances=utterances, language=language or "en")


class ElevenLabsSTT:
    """ElevenLabs Scribe over HTTP: one short utterance in, its text out.

    Used for practice mode, where the browser cuts speech into utterances with
    voice-activity detection and each one is transcribed as it ends.
    """

    name = "elevenlabs"

    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        self._key = api_key
        self._model = model
        self._base = base_url.rstrip("/")

    async def transcribe(self, audio: bytes, *, language: str | None = None) -> Transcription:
        import httpx

        data = {"model_id": self._model}
        if language:
            data["language_code"] = language
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self._base}/v1/speech-to-text",
                    headers={"xi-api-key": self._key},
                    data=data,
                    files={"file": ("utterance.wav", audio, "audio/wav")},
                )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"ElevenLabs STT failed: {exc}") from exc

        text = str(body.get("text", "")).strip()
        if not text:
            return Transcription(utterances=[], language=body.get("language_code") or "en")
        words = body.get("words") or []
        end = float(words[-1].get("end", 0.0)) if words else 0.0
        return Transcription(
            utterances=[Utterance(start=0.0, end=end, text=text, confidence=0.9)],
            language=body.get("language_code") or "en",
        )


class OpenAICompatibleSTT:
    """Any OpenAI-compatible `/audio/transcriptions`: one short utterance in.

    The same endpoint and key as the LLM (`OPENAI_BASE_URL`), so on Groq it is
    Whisper large-v3 under the organization already in use, and on OpenAI it is
    `whisper-1`. Nothing to install and no CPU spent on this machine.
    """

    name = "openai"

    def __init__(self, api_key: str, model: str, base_url: str) -> None:
        self._key = api_key
        self._model = model
        self._base = (base_url or "https://api.openai.com/v1").rstrip("/")

    async def transcribe(self, audio: bytes, *, language: str | None = None) -> Transcription:
        import httpx

        data = {"model": self._model, "response_format": "verbose_json"}
        if language:
            data["language"] = language
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(
                    f"{self._base}/audio/transcriptions",
                    headers={"Authorization": f"Bearer {self._key}"},
                    data=data,
                    files={"file": ("utterance.wav", audio, "audio/wav")},
                )
            response.raise_for_status()
            body = response.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise RuntimeError(f"speech-to-text failed: {exc}") from exc

        segments = body.get("segments") or []
        utterances = [
            Utterance(
                start=float(s.get("start", 0.0)),
                end=float(s.get("end", 0.0)),
                text=str(s.get("text", "")).strip(),
                confidence=_logprob_to_confidence(float(s.get("avg_logprob", -0.5))),
            )
            for s in segments
            if str(s.get("text", "")).strip()
        ]
        text = str(body.get("text", "")).strip()
        if not utterances and text:
            end = float(body.get("duration") or 0.0)
            utterances = [Utterance(start=0.0, end=end, text=text, confidence=0.9)]
        return Transcription(utterances=utterances, language=body.get("language") or "en")


class WhisperSTT:
    """Local Whisper via faster-whisper (design §54: "Whisper, run locally").

    The model is loaded lazily on first use and reused: loading takes seconds
    and must not happen inside a request. Transcription itself is compute-bound, so
    it runs in a worker thread rather than blocking the event loop — a slow
    transcription must never stall the live session's WebSocket traffic
    (claude.md invariant 1).
    """

    name = "whisper"

    def __init__(
        self, model_size: str = "base", device: str = "auto", compute_type: str = "default"
    ):
        # "auto" uses the GPU when CTranslate2 finds CUDA (float16 there) and
        # falls back to CPU otherwise, so the same config runs on any machine.
        self._model_size = model_size
        self._device = device
        self._compute_type = compute_type
        self._model = None
        self._lock = asyncio.Lock()

    async def _get_model(self):
        if self._model is None:
            async with self._lock:
                if self._model is None:
                    from faster_whisper import WhisperModel

                    logger.info(
                        "loading whisper model",
                        extra={"model": self._model_size, "device": self._device},
                    )
                    self._model = await asyncio.to_thread(
                        WhisperModel,
                        self._model_size,
                        device=self._device,
                        compute_type=self._compute_type,
                    )
        return self._model

    async def transcribe(self, audio: bytes, *, language: str | None = None) -> Transcription:
        import io

        model = await self._get_model()

        def _run() -> Transcription:
            segments, info = model.transcribe(
                io.BytesIO(audio),
                language=language,
                # Skip silence rather than transcribing it — chunks are wall
                # clock windows and are often mostly quiet for a given speaker.
                vad_filter=True,
                beam_size=5,
            )
            utterances = [
                Utterance(
                    start=float(s.start),
                    end=float(s.end),
                    text=s.text.strip(),
                    # faster-whisper reports avg log-probability; map it into
                    # the 0–1 confidence the transcript contract requires.
                    confidence=_logprob_to_confidence(getattr(s, "avg_logprob", -0.5)),
                )
                for s in segments
                if s.text and s.text.strip()
            ]
            return Transcription(utterances=utterances, language=info.language or "en")

        return await asyncio.to_thread(_run)


def _logprob_to_confidence(avg_logprob: float) -> float:
    """Map Whisper's average log-probability onto 0–1.

    exp(avg_logprob) is the geometric mean token probability, which is already
    a reasonable confidence and always lands in range.
    """
    import math

    return max(0.0, min(1.0, math.exp(avg_logprob)))


_provider: STTProvider | None = None


def get_stt_provider() -> STTProvider:
    """Process-wide provider, chosen from settings.

    `stt_provider=auto` prefers Whisper and falls back to the stub when
    faster-whisper is not installed, so the pipeline always runs.
    """
    global _provider
    if _provider is not None:
        return _provider

    choice = (settings.stt_provider or "auto").lower()
    if choice == "elevenlabs" or (choice == "auto" and settings.elevenlabs_api_key):
        _provider = ElevenLabsSTT(
            settings.elevenlabs_api_key, settings.elevenlabs_stt_model, settings.elevenlabs_base_url
        )
    elif choice == "openai":
        _provider = OpenAICompatibleSTT(
            settings.llm_api_key or settings.openai_api_key,
            settings.openai_stt_model,
            settings.openai_base_url,
        )
    elif choice == "stub":
        _provider = StubSTT()
    elif choice == "whisper":
        _provider = WhisperSTT(model_size=settings.whisper_model)
    else:
        try:
            import faster_whisper  # noqa: F401

            _provider = WhisperSTT(model_size=settings.whisper_model)
        except ImportError:
            logger.warning(
                "faster-whisper is not installed; falling back to the stub STT provider. "
                "Install services/requirements-speech.txt for real transcription."
            )
            _provider = StubSTT()

    logger.info("stt provider selected", extra={"provider": _provider.name})
    return _provider


def set_stt_provider(provider: STTProvider | None) -> None:
    """Override the provider. Used by tests and by the reprocessing worker."""
    global _provider
    _provider = provider
