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

Purpose: Speech-to-text behind one interface (STTProvider) with four implementations: local
    Whisper, ElevenLabs Scribe, any OpenAI-compatible endpoint, and a deterministic stub.
Why needed: The rest of the system only depends on STTProvider, so the transcription vendor can
    change, or be absent entirely, without touching business logic (design §54). The stub lets
    the whole pipeline and test suite run with no model download or network.
Contains:
  - Utterance / Transcription: the result shape, with times relative to the chunk start.
  - STTProvider: the one-method protocol everything depends on.
  - StubSTT: deterministic fake transcripts derived from a hash of the audio.
  - ElevenLabsSTT / OpenAICompatibleSTT: managed ASR over HTTP, one utterance at a time
    (practice mode).
  - WhisperSTT: local faster-whisper, loaded lazily and run in a worker thread.
  - _logprob_to_confidence: map Whisper's log-probability to a 0-1 confidence.
  - get_stt_provider / set_stt_provider: pick the process-wide provider from settings, or
    override it in tests.
Used by: services/ai_platform/skill_graph.py, services/practice/engine.py,
    services/speech/service.py, services/tests/conftest.py, services/tests/test_openai_stt.py,
    services/tests/test_panel.py, services/tests/test_platform.py,
    services/tests/test_practice.py, services/tests/test_speech_pipeline.py
Depends on: services/common/config.py
"""

import asyncio  # run blocking Whisper in a thread; lock for lazy model load
import hashlib  # stub transcripts are derived from a hash of the audio
import logging  # provider selection and model loading logs
from dataclasses import dataclass, field  # Utterance / Transcription containers
from typing import Protocol, runtime_checkable  # the STTProvider interface

from services.common.config import settings  # provider choice, keys, model names

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
    """The result of transcribing one audio chunk."""

    utterances: list[Utterance] = field(default_factory=list)
    language: str = "en"

    @property
    def text(self) -> str:
        """All utterances joined into one string."""
        return " ".join(u.text for u in self.utterances).strip()


@runtime_checkable
class STTProvider(Protocol):
    """The whole contract the rest of the system depends on."""

    name: str

    async def transcribe(self, audio: bytes, *, language: str | None = None) -> Transcription:
        """Turn audio bytes into timestamped text (times relative to the chunk start)."""
        ...


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
        """Return 1-3 canned phrases picked by the audio hash, evenly spread over the chunk."""
        # Hash the audio so identical bytes always give identical text (deterministic for tests).
        digest = hashlib.sha256(audio).digest()
        # Choose 1 to 3 utterances from the first hash byte.
        count = 1 + digest[0] % 3
        utterances = []
        # Spread the utterances evenly across the chunk.
        span = self._chunk_seconds / count
        # Each utterance picks its phrase and confidence from different hash bytes.
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
        """POST the utterance to Scribe and wrap the returned text as one utterance."""
        import httpx  # imported lazily so offline setups never need it

        # Scribe form fields: the model, plus an optional language hint.
        data = {"model_id": self._model}
        if language:
            data["language_code"] = language
        # Network or HTTP errors become a RuntimeError, which the caller records as a FAILED chunk.
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

        # Scribe returns one text blob plus per-word timings.
        text = str(body.get("text", "")).strip()
        if not text:
            return Transcription(utterances=[], language=body.get("language_code") or "en")
        # Use the last word's end time as the utterance length (0 if there are no word timings).
        words = body.get("words") or []
        end = float(words[-1].get("end", 0.0)) if words else 0.0
        # One utterance covering the whole clip, with a fixed confidence of 0.9.
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
        """POST the utterance to /audio/transcriptions and map its segments to utterances."""
        import httpx  # imported lazily so offline setups never need it

        # verbose_json includes per-segment timings and log-probabilities.
        data = {"model": self._model, "response_format": "verbose_json"}
        if language:
            data["language"] = language
        # Network or HTTP errors become a RuntimeError, which the caller records as a FAILED chunk.
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

        # Skip empty segments and convert Whisper's log-probability into a 0-1 confidence.
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
        # Some servers return only a text blob without segments; wrap it as one utterance.
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
        """Load the Whisper model once (double-checked under a lock) and reuse it."""
        # Double-checked locking: skip the lock once the model is loaded.
        if self._model is None:
            async with self._lock:
                # Re-check inside the lock in case another request loaded it meanwhile.
                if self._model is None:
                    # Imported lazily so the package is only needed when Whisper is actually used.
                    from faster_whisper import WhisperModel

                    logger.info(
                        "loading whisper model",
                        extra={"model": self._model_size, "device": self._device},
                    )
                    # Model loading is slow and blocking, so it runs in a worker thread.
                    self._model = await asyncio.to_thread(
                        WhisperModel,
                        self._model_size,
                        device=self._device,
                        compute_type=self._compute_type,
                    )
        return self._model

    async def transcribe(self, audio: bytes, *, language: str | None = None) -> Transcription:
        """Transcribe locally with faster-whisper, off the event loop."""
        import io  # wrap the bytes as a file-like object for the model

        # Load (or reuse) the model before starting the worker thread.
        model = await self._get_model()

        def _run() -> Transcription:
            """Blocking transcription, executed in a worker thread."""
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

        # Run the blocking transcription in a thread so the event loop stays responsive.
        return await asyncio.to_thread(_run)


def _logprob_to_confidence(avg_logprob: float) -> float:
    """Map Whisper's average log-probability onto 0–1.

    exp(avg_logprob) is the geometric mean token probability, which is already
    a reasonable confidence and always lands in range.
    """
    import math  # exp() to turn log-probability into probability

    return max(0.0, min(1.0, math.exp(avg_logprob)))


# Process-wide provider, created on first use by get_stt_provider().
_provider: STTProvider | None = None


def get_stt_provider() -> STTProvider:
    """Process-wide provider, chosen from settings.

    `stt_provider=auto` prefers Whisper and falls back to the stub when
    faster-whisper is not installed, so the pipeline always runs.
    """
    global _provider
    # Cached after first use: one provider per process.
    if _provider is not None:
        return _provider

    # Normalise the configured provider name; auto picks the best one available.
    choice = (settings.stt_provider or "auto").lower()
    # ElevenLabs when asked for, or when auto finds an API key.
    if choice == "elevenlabs" or (choice == "auto" and settings.elevenlabs_api_key):
        _provider = ElevenLabsSTT(
            settings.elevenlabs_api_key, settings.elevenlabs_stt_model, settings.elevenlabs_base_url
        )
    # OpenAI-compatible endpoint, reusing the LLM key and base URL.
    elif choice == "openai":
        _provider = OpenAICompatibleSTT(
            settings.llm_api_key or settings.openai_api_key,
            settings.openai_stt_model,
            settings.openai_base_url,
        )
    # Deterministic offline provider.
    elif choice == "stub":
        _provider = StubSTT()
    # Local faster-whisper.
    elif choice == "whisper":
        _provider = WhisperSTT(model_size=settings.whisper_model)
    # Auto fallback: use Whisper if installed, otherwise the stub, so the pipeline always runs.
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

    # Log the choice once so it is obvious which backend is in use.
    logger.info("stt provider selected", extra={"provider": _provider.name})
    return _provider


def set_stt_provider(provider: STTProvider | None) -> None:
    """Override the provider. Used by tests and by the reprocessing worker."""
    global _provider
    # None clears the cache so the next call re-selects.
    _provider = provider
