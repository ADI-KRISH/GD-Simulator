"""Each practice persona gets its own Edge voice and delivery."""

from services.practice.personas import PRESETS
from services.speech.tts import EdgeVoice, edge_voice_for


def test_every_preset_persona_has_a_distinct_voice_and_delivery():
    """Every preset persona maps to a different Edge voice setup."""
    chosen = {key: edge_voice_for(p.voice_id) for key, p in PRESETS.items()}
    assert len(set(chosen.values())) == len(PRESETS)


def test_personas_differ_in_pace_not_just_voice():
    """Personas differ in speaking pace as well as voice."""
    assert edge_voice_for(PRESETS["dev"].voice_id).rate.startswith("+")
    assert edge_voice_for(PRESETS["esha"].voice_id).rate.startswith("-")


def test_a_bare_edge_voice_name_is_used_as_is():
    """A plain Edge voice name is used unchanged."""
    assert edge_voice_for("en-IN-PrabhatNeural") == EdgeVoice("en-IN-PrabhatNeural")


def test_an_unknown_voice_id_falls_back_to_an_indian_voice():
    """An unknown voice id falls back to an Indian-English voice."""
    assert edge_voice_for("not-a-real-id").voice.startswith("en-IN-")
