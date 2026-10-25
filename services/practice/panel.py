"""How the AI panel behaves as a panel: stances, a moderator, turn-taking, rules.

Purpose:     Holds every decision the AI panel makes during practice: what
             stance and angle each persona takes, who moderates, who speaks next
             (a bidding system), how a persona's prompt is built, whether a line
             repeats an earlier point, and the guarantee that a persona admits
             being an AI when asked.
Why needed:  Keeping these as pure functions (no I/O) means each rule can be unit
             tested on its own, while the engine only runs the conversation loop.
             Without this module the personas would agree politely, talk in
             circles, and could claim to be human or invent statistics.
Contains:
  - Constants: `STUDENT`, `STANCES`, `ANGLES`, `RULES`, thresholds
    (`REDIRECT_AFTER_SECONDS`, `DOMINANT_SHARE`, `MAX_REDIRECTS`,
    `YIELDS_BEFORE_CURVEBALL`), and the stop-word list / "are you an AI?" regexes.
  - `sample_stances`, `sample_angles`   give each persona a distinct view.
  - `pick_moderator`                    choose who also runs the room.
  - `stance_text`, `angle_text`, `cue_text`, `persona_system`  build the prompt.
  - `content_words`, `repeats`          detect a restated point.
  - `bid`, `choose_next`                the floor-bidding turn-taking.
  - `should_redirect`, `quietest`       stop the student hogging the floor.
  - `asked_if_ai`, `ensure_honest`      the deterministic honesty guard.
Used by:     practice/engine.py (the conversation loop), practice/personas.py,
             services/simulation.py, tests/test_panel.py.
Depends on:  nothing in this project (standard library only).

The practice engine owns the conversation loop; this module owns the decisions
inside it, as pure functions with no I/O, so each one is testable on its own:

* **Stances and angles** (M14 FR-14.4). Every persona is given a stance on the
  topic and a private angle — the lens it keeps returning to — sampled so the
  panel disagrees instead of agreeing politely.
* **A moderator** (FR-14.3, EC-14.1, EC-14.2). One persona also runs the room:
  it opens and invites the student, brings a silent student in, hands the floor
  on when one voice holds it, and closes.
* **Bids for the floor** (FR-14.6). Each persona bids for the next turn —
  eagerness × freshness × disagreement × relevance × floor balance — and the
  highest bid speaks.
* **No repeats** (FR-14.5, EC-14.4). A line that restates a recent point is
  sent back once; repeated again, the persona yields, and three yields in a row
  bring a curveball from the moderator.
* **Rules every AI participant keeps** (AC-14.5, EC-14.14). It says it is an AI
  when asked — with a deterministic guard behind the prompt — and never invents
  statistics or sources: a practice tool must not teach students to quote fake
  numbers.
"""

from __future__ import annotations

import random  # shuffle stances/angles; jitter bids
import re  # word extraction and "are you an AI?" detection
from collections.abc import Mapping  # read-only dict-like persona/floor inputs

# The student's transcript label in practice mode (personas are P1, P2, ...).
STUDENT = "A"

# The positions a persona can take on the topic.
STANCES = ("support", "oppose", "it depends")
# Prompt wording for each stance.
_STANCE_TEXT = {
    "support": "You broadly SUPPORT the motion.",
    "oppose": "You broadly OPPOSE the motion.",
    "it depends": "You think IT DEPENDS — on who, how and under what conditions.",
}

#: Private angles: the lens a persona keeps coming back to. Sampled without
#: replacement so no two personas argue from the same place.
ANGLES: tuple[tuple[str, str], ...] = (
    ("cost", "who pays, what it costs, and what that money could do instead"),
    ("fairness", "who gets left out, and whether it is fair to everyone it touches"),
    ("employers", "what employers actually need, and how hiring really works"),
    ("students", "what it is like for the people it happens to: workload, stress, learning"),
    ("practicality", "how it would be put into practice, and what would go wrong"),
    ("the long run", "what this leads to in five or ten years"),
)

#: Kept by every AI participant, in practice and in simulated group rooms.
RULES = (
    "Ground rules you never break:\n"
    "- You are an AI participant playing a character in a practice group discussion. "
    "If anyone asks whether you are an AI, a bot or a real person, say plainly in one "
    "short sentence that you are an AI participant, then carry on. Never claim to be human.\n"
    "- Never invent statistics, studies, surveys, percentages, dates, quotes or named "
    "sources. Argue from reasoning, trade-offs and widely known facts. When you need an "
    "example, make it clearly hypothetical ('say a student in a small town...'). If you "
    "are not sure a figure is real, leave the figure out.\n"
    "- Do not repeat a point someone has already made. If you agree, add what is new."
)

# Words in a persona brief that mark it as a natural moderator.
_MODERATOR_HINTS = ("moderat", "summaris", "summariz", "bridges", "invites quieter")

#: Seconds of talk before floor shares mean anything.
REDIRECT_AFTER_SECONDS = 45.0
#: The student's share of all talk above which the moderator hands the floor on.
DOMINANT_SHARE = 0.70
#: EC-14.2: the moderator intervenes twice, no more.
MAX_REDIRECTS = 2
#: Repetitions in a row before the moderator throws in a curveball (EC-14.4).
YIELDS_BEFORE_CURVEBALL = 3

# Common words ignored when deciding whether two lines say the same thing.
_STOPWORDS = frozenset(
    """a an the and or but if so to of in on at for with by from as is are was were be
    been being it its this that these those i you he she we they me him her us them my
    your our their not no yes do does did have has had will would can could should may
    might must just also very really then than there here what which who whom whose why
    how all any some more most such only own same too about into over under again once
    think say said like well yeah okay right look see get got make made going want know
    people thing things lot way something because even still much many""".split()
)

# "Are you a bot / AI / human / real person?" in its common phrasings.
_ASKED_IF_AI = re.compile(
    r"\b(are|r)\s+(you|u)\s+(an?\s+)?(bot|robot|ai|a\.i\.?|chatbot|human|real(\s+person)?|"
    r"machine|computer|program)\b",
    re.IGNORECASE,
)
# A reply that already admits being an AI.
_ADMITS_AI = re.compile(r"\b(ai|a\.i\.|artificial|bot|not (a )?human)\b", re.IGNORECASE)


# --------------------------------------------------------------- the panel


def sample_stances(count: int, rng: random.Random) -> list[str]:
    """Stances for `count` personas: never all alike, distinct up to three.

    AC-14.3 asks for divergence in every session. Up to three personas get three
    different stances; a fourth repeats one, and its angle keeps it distinct.
    """
    if count <= 0:
        return []
    # Shuffle the three distinct stances so who gets which one varies each session.
    picked = list(STANCES)
    rng.shuffle(picked)
    # More personas than stances: top up with random repeats.
    while len(picked) < count:
        picked.append(rng.choice(STANCES))
    return picked[:count]


def sample_angles(count: int, rng: random.Random) -> list[str]:
    """One private angle per persona, no two the same."""
    # Shuffle the angle names; each persona then takes the next unused one.
    names = [name for name, _ in ANGLES]
    rng.shuffle(names)
    # Only if there are more personas than angles do angles repeat.
    while len(names) < count:
        names.append(rng.choice([name for name, _ in ANGLES]))
    return names[:count]


def pick_moderator(personas: Mapping[str, Mapping]) -> str | None:
    """The persona who also runs the room: a natural moderator, else the first."""
    for label, persona in personas.items():
        brief = str(persona.get("persona", "")).lower()
        # An explicit moderator role, or a brief that reads like a moderator's, wins.
        if persona.get("role") == "moderator" or any(h in brief for h in _MODERATOR_HINTS):
            return label
    # Nobody fits: default to the first persona (None if the panel is empty).
    return next(iter(personas), None)


# ----------------------------------------------------------------- prompts


def stance_text(stance: str | None) -> str:
    """Prompt sentence for a stance ("" if none)."""
    return _STANCE_TEXT.get(stance or "", "")


def angle_text(angle: str | None) -> str:
    """Prompt text "name — description" for an angle ("" if unknown)."""
    # Look the angle up by name and render "name — description".
    for name, description in ANGLES:
        if name == angle:
            return f"{name} — {description}"
    return ""


def cue_text(
    cue: str | None, *, student: str, target: str = "", repeated: str = "", named: bool = True
) -> str:
    """The instruction for one special turn. `named`: the student has a name to say."""
    if not cue:
        return ""
    # If we don't know the student's name, don't ask the persona to say it.
    by_name = " by name" if named else ""
    # One instruction per special turn: open, invite, hand, redirect, curveball, close, fresh.
    if cue == "open":
        return (
            "YOUR TASK NOW: you are opening the discussion. In one or two sentences, put the "
            f"topic in plain words, then invite {student}{by_name} to give their view first. "
            "Do not argue your own position yet."
        )
    if cue == "invite":
        return (
            f"YOUR TASK NOW: {student} has not spoken for a while. In one sentence, invite "
            f"them in{by_name} with a short, open question about the point just made. Do not "
            "answer it yourself."
        )
    if cue == "hand":
        return (
            f"YOUR TASK NOW: {student} has raised their hand. In one short sentence, invite "
            f"them{by_name} to speak. Do not add a point of your own."
        )
    if cue == "redirect":
        return (
            f"YOUR TASK NOW: {student} has held most of the floor. Thank them briefly and hand "
            f"the floor to {target} by name with a short question. One sentence."
        )
    if cue == "curveball":
        return (
            "YOUR TASK NOW: the discussion is going in circles. Raise one genuinely new "
            "question or angle nobody has mentioned yet, in one or two sentences, and ask the "
            "group to respond."
        )
    if cue == "close":
        return (
            "YOUR TASK NOW: as the moderator, close the discussion: what the group agrees on, "
            "where it still differs, and one closing recommendation. Set is_conclusion to true."
        )
    if cue == "fresh":
        return (
            f'Your previous draft repeated a point already made: "{repeated}". Say something '
            "genuinely new — a different reason, example or question — or set speak to false."
        )
    return ""


def persona_system(
    persona: Mapping,
    *,
    student: str,
    others: str,
    topic: str,
    max_words: int,
    clock_guidance: str,
    is_moderator: bool,
    cue: str | None = None,
    target: str = "",
    repeated: str = "",
    asked_ai: bool = False,
    named: bool = True,
) -> str:
    """The whole system prompt for one persona line.

    `asked_ai`: the student's last line asked whether we are an AI. Measured
    against Groq on 2026-10-17, the standing rule alone got an admission 2 times
    in 5; the turn-specific instruction is what makes the model say it, and
    `ensure_honest` stays behind it as the guarantee.
    """
    # Build the prompt as a list of paragraphs: identity, topic, character first...
    parts = [
        f"You are {persona['name']}, taking part in a live spoken group discussion with "
        f"{student} (a student practising) and {others or 'nobody else'}.",
        f"Topic: {topic}",
        f"Your persona:\n{persona['persona']}",
    ]
    # ...then the view this persona holds and the lens it keeps returning to.
    if stance := stance_text(persona.get("stance")):
        parts.append(
            f"Your stance: {stance} Hold it unless you are genuinely persuaded; concede a "
            "specific point rather than switching sides."
        )
    if angle := angle_text(persona.get("angle")):
        parts.append(f"Your angle, the thing you know most about and come back to: {angle}.")
    # The moderator keeps the room fair on top of taking part.
    if is_moderator:
        parts.append(
            "You also keep this discussion fair: you open it, make sure everyone — "
            f"{student} above all — gets their turn, and close it. Between those moments you "
            "take part like anyone else."
        )
    # Stops personas inventing a name for an anonymous student.
    if not named:
        parts.append(
            "You do not know the student's name. When you address them, say 'you'; never "
            "call them by a name you make up, and never mistake them for one of the panel."
        )
    # The honesty rules, then how to sound spoken (short, natural, within the word cap).
    parts.append(RULES)
    parts.append(
        "Speak the way people really talk, because your words are read out loud: short "
        "sentences, contractions, the odd 'look', 'right' or 'see', and no essay-style "
        "phrasing. No lists, markdown, stage directions or name prefix. Keep it to at most "
        f"{max_words} words. React to what was just said. If the student has not spoken yet, "
        "open with a clear point of view. Stay aware of the clock: you may mention time "
        "naturally (for example 'we are running short') but never contradict it."
    )
    # Time awareness (from timing.py), then any turn-specific instruction.
    parts.append(clock_guidance)
    if asked_ai:
        parts.append(
            f"YOUR TASK NOW: {student} just asked whether you are an AI. In your first few "
            "words, say plainly that you are an AI participant, then carry on with the "
            "discussion."
        )
    if instruction := cue_text(
        cue, student=student, target=target, repeated=repeated, named=named
    ):
        parts.append(instruction)
    # Last, the exact JSON shape the engine expects back.
    parts.append(
        'Return JSON: {"speak": <bool>, "is_conclusion": <bool>, "text": "<what you say next>"}.'
    )
    return "\n\n".join(parts)


# --------------------------------------------------------- the floor


def content_words(text: str) -> set[str]:
    """The meaningful lowercase words of a line (no stopwords, 3+ letters)."""
    return {
        word
        for word in re.findall(r"[a-z][a-z']+", text.lower())
        if len(word) > 2 and word not in _STOPWORDS
    }


def repeats(text: str, earlier: list[str], *, min_shared: int = 5, share: float = 0.6) -> str:
    """The earlier line this one restates, or "" if it says something new."""
    words = content_words(text)
    # Too short to judge meaningfully: never call it a repeat.
    if len(words) < min_shared:
        return ""
    # Compare against recent lines (newest first); a repeat shares enough of the
    # smaller line's meaningful words.
    for line in reversed(earlier):
        other = content_words(line)
        common = words & other
        if len(common) >= min_shared and len(common) / min(len(words), len(other)) >= share:
            return line
    return ""


def bid(
    persona: Mapping,
    *,
    turns_since: int,
    floor_share: float,
    last_text: str,
    last_stance: str | None,
    is_moderator: bool,
    closing: bool,
    rng: random.Random,
) -> float:
    """How strongly one persona wants the next turn."""
    value = float(persona.get("eagerness", 1.0))
    # A fresh voice bids highest: someone who has not spoken for six turns (or ever)
    # triples their bid.
    value *= 1.0 + min(turns_since, 6) / 3.0
    # Disagreement draws a reply.
    stance = persona.get("stance")
    if last_stance and stance and stance != last_stance:
        value *= 1.5
    # Relevance: the last line touched this persona's angle.
    angle_words = content_words(angle_text(persona.get("angle")))
    if angle_words and angle_words & content_words(last_text):
        value *= 1.4
    # Floor balance: the more of the talk already held, the lower the bid.
    value /= 1.0 + 2.0 * floor_share
    if is_moderator:
        value *= 6.0 if closing else 0.6
    return value * rng.uniform(0.85, 1.15)


def choose_next(
    personas: Mapping[str, Mapping],
    *,
    last_speaker: str | None,
    last_text: str,
    turn: int,
    last_turn_of: Mapping[str, int],
    floor: Mapping[str, float],
    moderator: str | None,
    closing: bool,
    rng: random.Random,
) -> str | None:
    """The persona with the highest bid; never the one who just spoke."""
    # Candidates for the next turn: everyone except the last speaker (unless alone).
    pool = [label for label in personas if label != last_speaker] or list(personas)
    if not pool:
        return None
    # `total` turns raw talk-time into shares; `last_stance` lets disagreement draw a reply.
    total = sum(floor.values()) or 1.0
    last_stance = personas.get(last_speaker or "", {}).get("stance")
    # Each candidate bids; the loudest bid gets the floor.
    bids = {
        label: bid(
            personas[label],
            turns_since=turn - last_turn_of.get(label, -6),
            floor_share=floor.get(label, 0.0) / total,
            last_text=last_text,
            last_stance=last_stance,
            is_moderator=label == moderator,
            closing=closing,
            rng=rng,
        )
        for label in pool
    }
    return max(bids, key=bids.__getitem__)


def should_redirect(floor: Mapping[str, float], redirects: int) -> bool:
    """EC-14.2: the student holds most of the talk; hand the floor on, at most twice."""
    total = sum(floor.values())
    # Already redirected twice, or not enough talk yet for shares to mean anything.
    if redirects >= MAX_REDIRECTS or total < REDIRECT_AFTER_SECONDS:
        return False
    # Redirect only if the student has more than DOMINANT_SHARE of all talk.
    return floor.get(STUDENT, 0.0) / total > DOMINANT_SHARE


def quietest(personas: Mapping[str, Mapping], floor: Mapping[str, float], exclude: str | None):
    """The persona who has held the floor least — the one to hand it to."""
    pool = [label for label in personas if label != exclude] or list(personas)
    return min(pool, key=lambda label: floor.get(label, 0.0)) if pool else None


# ------------------------------------------------------ honesty, enforced


def asked_if_ai(text: str) -> bool:
    """True if the text asks whether the speaker is an AI / bot / human."""
    return bool(_ASKED_IF_AI.search(text))


def ensure_honest(reply: str, question: str) -> str:
    """AC-14.5 is a gate: when asked, the reply says it is an AI — every time.

    The rule in the prompt does the work; this guarantees it when a model forgets.
    """
    # Asked "are you an AI?" but the reply doesn't say so: prepend the admission.
    if asked_if_ai(question) and not _ADMITS_AI.search(reply):
        return f"I'm an AI participant, to be clear. {reply}".strip()
    return reply
