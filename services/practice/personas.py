"""AI discussion personas for solo practice.

Purpose:     Defines the six built-in AI discussion partners (voice, speaking
             style, brief) and turns a practice request (presets or custom
             personas) into the stored rows a practice session uses.
Why needed:  Practice mode needs believable, distinct opponents. Centralising them
             here keeps their personalities and voices in one editable place, and
             `resolve_personas` gives every panel member a sampled stance so the
             group disagrees instead of nodding along.
Contains:
  - `Persona`            a frozen persona definition (+ `public` for the API).
  - `PRESETS`            the six ready-made personas, keyed by name.
  - `PERSONA_LABEL_PREFIX`  "P" prefix so persona labels (P1..) never clash with
                         candidate letters (A, B, ...).
  - `resolve_personas`   validate a request, apply voice overrides, assign
                         labels, stances and angles.
Used by:     practice/router.py (lists personas and builds sessions),
             services/simulation.py, tests/test_panel.py, tests/test_practice.py,
             tests/test_edge_voices.py.
Depends on:  common/config.py (per-persona voice overrides) and
             practice/panel.py (`sample_stances`, `sample_angles`).

Each persona is a voice (an ElevenLabs premade voice id), a speaking style and a
short brief the LLM plays. They are non-scored speakers: they share the floor
with the student and are tracked like anyone else, but only the student is
evaluated.
"""

import random  # seedable RNG for stance/angle sampling
from dataclasses import asdict, dataclass  # Persona container and its dict form

from services.common.config import settings  # per-persona voice overrides
from services.practice.panel import sample_angles, sample_stances  # give each persona a view


@dataclass(frozen=True, slots=True)
class Persona:
    """A built-in AI discussion partner: identity, brief for the LLM, and voice."""

    key: str
    name: str
    tagline: str
    brief: str
    voice_id: str
    # "f" or "m": picks the matching browser voice when ElevenLabs is unavailable.
    gender: str = "m"
    # How eagerly this persona jumps in: scales the pick weight (0.5-2.0).
    eagerness: float = 1.0

    def public(self) -> dict:
        """The persona as a plain dict for the API."""
        return asdict(self)


# The six ready-made personas, keyed by `key`. Positional args: key, name, tagline,
# brief, voice_id, then (optionally) gender and eagerness.
PRESETS: dict[str, Persona] = {
    p.key: p
    for p in (
        Persona(
            "arjun",
            "Arjun",
            "The analyst",
            "Analytical and precise. Reasons step by step, tests what each claim rests "
            "on, and asks others for their evidence - but never makes up a number.",
            "cjVigY5qzO86Huf0OWal",  # Eric: smooth, trustworthy
        ),
        Persona(
            "bhavana",
            "Bhavana",
            "The devil's advocate",
            "Challenges every claim, politely but firmly. Finds the weak assumption "
            "in what was just said and presses on it.",
            "XrExE9yKIg1WjnnlVkGX",  # Matilda: knowledgeable, upbeat
            "f",
            1.3,
        ),
        Persona(
            "chitra",
            "Chitra",
            "The diplomat",
            "Calm moderator. Summarises where the group has got to, bridges "
            "disagreements and invites quieter voices in.",
            "EXAVITQu4vr4xnSDxMaL",  # Sarah: mature, reassuring
            "f",
        ),
        Persona(
            "dev",
            "Dev",
            "The dominator",
            "Confident and fast-talking. Pushes his own view hard, tends to talk over "
            "points and wants to steer the discussion. Never rude, just forceful.",
            "IKne3meq5aSn9XLyUdCD",  # Charlie: deep, confident, energetic
            "m",
            1.8,
        ),
        Persona(
            "esha",
            "Esha",
            "The quiet thinker",
            "Speaks rarely but makes one sharp, original point at a time. Needs to be "
            "drawn out.",
            "pFZP5JQG7iQjIQuC4Bku",  # Lily: velvety, soft
            "f",
            0.5,
        ),
        Persona(
            "farhan",
            "Farhan",
            "The industry expert",
            "Brings real-world examples and case studies from business and policy. "
            "Practical rather than theoretical.",
            "JBFqnCBsd6RMkjVDRZzb",  # George: warm storyteller
        ),
    )
}

# Transcript/turn labels for personas. Candidates are single letters (A, B...),
# so "P1".."P4" can never collide with one.
PERSONA_LABEL_PREFIX = "P"


def resolve_personas(requested: list[dict], rng: random.Random | None = None) -> list[dict]:
    """Turn a request ({preset} or custom {name, persona, voice_id}) into stored rows.

    Each row also gets a sampled stance and private angle (panel.py), so a panel
    disagrees instead of agreeing politely. Raises ValueError for an unknown preset
    or an empty custom persona.
    """
    rows: list[dict] = []
    for index, item in enumerate(requested, start=1):
        # Either a known preset, or a custom persona the student described.
        preset = PRESETS.get(str(item.get("preset", "")).lower())
        # Naming a preset that doesn't exist is an error, not a silent fallback.
        if item.get("preset") and preset is None:
            raise ValueError(f"unknown persona preset {item['preset']!r}")
        # Preset: copy its definition, letting .env swap the voice.
        if preset:
            row = {
                "name": preset.name,
                "persona": preset.brief,
                # PERSONA_VOICES in .env swaps a preset's voice (e.g. for an Indian one).
                "voice_id": settings.persona_voices.get(preset.key, preset.voice_id),
                "gender": preset.gender,
                "eagerness": preset.eagerness,
            }
        else:
            # Custom persona: needs a name and a description; fields are length-capped below.
            name = str(item.get("name", "")).strip()
            brief = str(item.get("persona", "")).strip()
            if not name or not brief:
                raise ValueError("a custom persona needs a name and a persona description")
            row = {
                "name": name[:60],
                "persona": brief[:1000],
                "voice_id": str(item.get("voice_id") or PRESETS["chitra"].voice_id),
                "gender": str(item.get("gender") or "f")[:1].lower(),
                "eagerness": 1.0,
            }
        # Give each persona a transcript label (P1, P2, ...) in request order.
        rows.append({"label": f"{PERSONA_LABEL_PREFIX}{index}", **row})
    # Assign every persona a sampled stance and angle so the panel genuinely disagrees.
    rng = rng or random.Random()
    for row, stance, angle in zip(
        rows, sample_stances(len(rows), rng), sample_angles(len(rows), rng), strict=True
    ):
        row["stance"], row["angle"] = stance, angle
    return rows
