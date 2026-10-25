"""
Purpose: Marks services/speech as a package: audio ingestion, speech-to-text, text-to-speech and
    transcript storage.
Why needed: Groups the whole path from a microphone chunk to a stored transcript segment,
    keeping vendors (Whisper, ElevenLabs) behind interfaces so they can be swapped.
Contains:
  - (no code) only this docstring; see service.py, stt.py, tts.py, chunking.py, diarization.py,
    models.py, repository.py and router.py.
Used by: services/metrics/service.py, services/observer/service.py, services/practice/engine.py,
    services/simulation.py, services/speech/router.py, services/speech/service.py,
    services/tests/conftest.py, services/tests/test_observer.py,
    services/tests/test_openai_stt.py, services/tests/test_speech_pipeline.py
Depends on: nothing inside the project (standard library / third-party only)
"""
