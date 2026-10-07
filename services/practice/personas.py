"""AI discussion personas for solo practice.

Each persona is a voice (an ElevenLabs premade voice id), a speaking style and a
short brief the LLM plays. They are non-scored speakers: they share the floor
with the student and are tracked like anyone else, but only the student is
evaluated.
"""

from dataclasses import asdict, dataclass

from services.common.config import settings


@dataclass(frozen=True, slots=True)
class Persona:
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
        return asdict(self)


PRESETS: dict[str, Persona] = {
    p.key: p
    for p in (
        Persona(
            "arjun",
            "Arjun",
            "The analyst",
            "Data-driven and precise. Backs every point with a number, a study or a "
            "concrete example, and asks others for their evidence.",
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


def resolve_personas(requested: list[dict]) -> list[dict]:
    """Turn a request ({preset} or custom {name, persona, voice_id}) into stored rows.

    Raises ValueError for an unknown preset or an empty custom persona.
    """
    rows: list[dict] = []
    for index, item in enumerate(requested, start=1):
        preset = PRESETS.get(str(item.get("preset", "")).lower())
        if item.get("preset") and preset is None:
            raise ValueError(f"unknown persona preset {item['preset']!r}")
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
        rows.append({"label": f"{PERSONA_LABEL_PREFIX}{index}", **row})
    return rows
