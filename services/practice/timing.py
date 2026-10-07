"""Clock awareness for the AI personas.

A real participant knows how long the discussion has run and how long is left:
they open with a position, build the argument in the middle, converge as time
runs short, and stay quiet once someone has already wrapped up. This module turns
the session clock into a phase and the guidance each phase implies, so the
personas' prompts carry it. Pure functions, no I/O.
"""

import re
from dataclasses import dataclass

OPENING = "opening"
MIDDLE = "middle"
WRAP_UP = "wrap_up"
FINAL = "final"
OVER = "over"

# Share of the discussion treated as the opening.
OPENING_SHARE = 0.15
# Converging starts this long before the end (or at 20% of a long session).
WRAP_UP_SECONDS = 60.0
WRAP_UP_SHARE = 0.20
# Last stretch: only a one-line close, and only if nobody has concluded yet.
FINAL_SECONDS = 20.0
# Too late to start a new line of speech that must also be spoken aloud.
TOO_LATE_SECONDS = 5.0

MAX_WORDS = {OPENING: 45, MIDDLE: 45, WRAP_UP: 60, FINAL: 22}

_CONCLUDING = re.compile(
    r"\b(to conclude|in conclusion|to sum up|to summari[sz]e|in summary|"
    r"to wrap up|to close|overall,? (?:i|we)|my final (?:point|thought))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Timing:
    elapsed: float
    remaining: float
    duration: float
    phase: str

    @property
    def converging(self) -> bool:
        return self.phase in (WRAP_UP, FINAL)


def timing_for(elapsed: float, duration: float) -> Timing:
    remaining = max(0.0, duration - elapsed)
    if remaining <= TOO_LATE_SECONDS:
        phase = OVER
    elif remaining <= FINAL_SECONDS:
        phase = FINAL
    elif remaining <= max(WRAP_UP_SECONDS, WRAP_UP_SHARE * duration):
        phase = WRAP_UP
    elif elapsed < OPENING_SHARE * duration:
        phase = OPENING
    else:
        phase = MIDDLE
    return Timing(elapsed, remaining, duration, phase)


def looks_like_conclusion(text: str) -> bool:
    """Did the speaker explicitly wrap the discussion up?"""
    return bool(_CONCLUDING.search(text))


def _clock(seconds: float) -> str:
    seconds = int(round(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s"


def guidance(timing: Timing, concluded: bool) -> str:
    """The time block of a persona's prompt."""
    head = (
        f"CLOCK: {_clock(timing.elapsed)} elapsed of {_clock(timing.duration)}; "
        f"{_clock(timing.remaining)} left. Phase: {timing.phase.replace('_', ' ')}. "
        f"Has someone already concluded the discussion? {'YES' if concluded else 'no'}.\n"
    )
    if timing.phase == OPENING:
        body = (
            "The discussion has just started. State your position with one clear reason. "
            "Do not summarise and do not try to conclude."
        )
    elif timing.phase == MIDDLE:
        body = (
            "Plenty of time remains. Respond to the previous speaker and add one new angle or "
            "piece of evidence. Do not summarise the whole discussion or try to conclude yet."
        )
    elif timing.phase == WRAP_UP:
        body = "Time is nearly up, so converge: stop opening new lines of argument. " + (
            "Someone has ALREADY concluded, so do not conclude again or repeat it. Set "
            "speak to false unless a very short agreement (12 words or fewer) truly adds "
            "something."
            if concluded
            else "Nobody has concluded yet. If you are the natural person to do it, give a "
            "brief conclusion: what the group agrees on, where it still differs, and one "
            "closing recommendation. Set is_conclusion to true when you do."
        )
    else:  # FINAL
        body = "Almost out of time. " + (
            "Someone has already concluded: set speak to false."
            if concluded
            else "Nobody has concluded: give ONE closing sentence (at most 20 words) and set "
            "is_conclusion to true."
        )
    return head + body
