"""Clock awareness for the AI personas.

Purpose:     Turns "how many seconds have passed in a discussion of N seconds"
             into a named phase (opening, middle, wrap-up, final, over) and the
             time-related instructions each phase implies for the AI personas.
Why needed:  Without it, personas would argue at full intensity until the buzzer,
             several would try to conclude at once, and nobody would stay quiet
             after a wrap-up. This module gives the panel a shared sense of time.
Contains:
  - Phase names and tuning constants (`OPENING`... `OVER`, `OPENING_SHARE`,
    `WRAP_UP_*`, `FINAL_SECONDS`, `TOO_LATE_SECONDS`, `LINE_SECONDS`,
    `MAX_WORDS`)  where the phases start and how long a spoken line is.
  - `_CONCLUDING`        regex for phrases like "to conclude" that signal a wrap-up.
  - `Timing`             a clock snapshot (+ `remaining_at_wrap_up`,
                         `lines_left`, `converging`).
  - `timing_for`         classify a moment into a `Timing`.
  - `looks_like_conclusion`  did the speaker explicitly conclude?
  - `_clock`             format seconds for the prompt.
  - `guidance`           the clock block of a persona's prompt, per phase.
Used by:     practice/engine.py (imported as `clock`) and tests/test_practice.py.
Depends on:  nothing in this project (standard library only).

A real participant knows how long the discussion has run and how long is left:
they open with a position, build the argument in the middle, converge as time
runs short, and stay quiet once someone has already wrapped up. This module turns
the session clock into a phase and the guidance each phase implies, so the
personas' prompts carry it. Pure functions, no I/O.
"""

import re  # detect phrases that signal a conclusion
from dataclasses import dataclass  # immutable Timing snapshot

# The five phases a discussion moves through, in order.
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
# Wrap-up never takes more than this share of a short session, so one still has a middle.
WRAP_UP_MAX_SHARE = 0.40
# Seconds one line costs the panel: ~35 words spoken plus the wait before it starts.
LINE_SECONDS = 14.0

# Short lines are faster to generate and to say, and sound like conversation.
MAX_WORDS = {OPENING: 35, MIDDLE: 35, WRAP_UP: 50, FINAL: 20}

# A line must be finished before the buzzer, so its length is capped by what fits
# in the time left: spoken words per second, and a margin kept clear at the end.
SPEECH_WORDS_PER_SECOND = 2.5
END_MARGIN_SECONDS = 4.0
MIN_WORDS_FITTED = 8

# Moderator time checks: (name, due once this phase is reached or this share has passed).
HALFWAY_SHARE = 0.5

# Phrases like "to conclude" / "my final point" that mark a deliberate wrap-up.
_CONCLUDING = re.compile(
    r"\b(to conclude|in conclusion|to sum up|to summari[sz]e|in summary|"
    r"to wrap up|to close|overall,? (?:i|we)|my final (?:point|thought))\b",
    re.IGNORECASE,
)


@dataclass(frozen=True, slots=True)
class Timing:
    """Where the discussion is on the clock, as numbers plus a named phase."""

    elapsed: float
    remaining: float
    duration: float
    phase: str

    @property
    def remaining_at_wrap_up(self) -> float:
        """Seconds from the end at which wrap-up begins for this session length."""
        return min(
            max(WRAP_UP_SECONDS, WRAP_UP_SHARE * self.duration), WRAP_UP_MAX_SHARE * self.duration
        )

    @property
    def lines_left(self) -> int:
        """How many more lines the whole panel can still say."""
        return int(self.remaining // LINE_SECONDS)

    @property
    def converging(self) -> bool:
        """True once the panel should be wrapping up rather than opening new arguments."""
        return self.phase in (WRAP_UP, FINAL)


def timing_for(elapsed: float, duration: float) -> Timing:
    """Classify the moment `elapsed` seconds into a `duration`-second discussion."""
    remaining = max(0.0, duration - elapsed)
    # Work backwards from the end: the closest-to-the-end phases are checked first.
    if remaining <= TOO_LATE_SECONDS:
        phase = OVER
    elif remaining <= FINAL_SECONDS:
        phase = FINAL
    elif remaining <= min(
        max(WRAP_UP_SECONDS, WRAP_UP_SHARE * duration), WRAP_UP_MAX_SHARE * duration
    ):
        phase = WRAP_UP
    # Otherwise it is either still the opening stretch or the long middle.
    elif elapsed < OPENING_SHARE * duration:
        phase = OPENING
    else:
        phase = MIDDLE
    return Timing(elapsed, remaining, duration, phase)


def word_budget(timing: Timing) -> int:
    """Most words a line may have: the phase's cap, cut down to what fits before the end.

    Without this a 35-word line (about 14 s) started with 10 s left is cut off by
    the buzzer, and the conclusion the whole panel was steering toward never lands.
    """
    cap = MAX_WORDS.get(timing.phase, 45)
    fits = int((timing.remaining - END_MARGIN_SECONDS) * SPEECH_WORDS_PER_SECOND)
    return max(MIN_WORDS_FITTED, min(cap, fits))


def time_check_due(timing: Timing, announced: set[str]) -> str | None:
    """Which spoken time check the moderator owes now ("half" or "wrap"), if any.

    Each is owed once. Missing a window (the room was busy) is fine: the later
    check is still owed, and an earlier one that is no longer true is dropped.
    """
    if timing.phase in (FINAL, OVER):
        return None
    if timing.phase == WRAP_UP:
        return "wrap" if "wrap" not in announced else None
    if timing.elapsed >= HALFWAY_SHARE * timing.duration and "half" not in announced:
        return "half"
    return None


def remaining_phrase(timing: Timing) -> str:
    """The time left, rounded the way a person would say it."""
    seconds = int(timing.remaining)
    if seconds >= 90:
        return f"about {round(seconds / 60)} minutes"
    return f"about {max(10, round(seconds / 10) * 10)} seconds"


def looks_like_conclusion(text: str) -> bool:
    """Did the speaker explicitly wrap the discussion up?"""
    return bool(_CONCLUDING.search(text))


def _clock(seconds: float) -> str:
    """Format seconds as e.g. "2m 05s" for the prompt."""
    seconds = int(round(seconds))
    return f"{seconds // 60}m {seconds % 60:02d}s"


def guidance(timing: Timing, concluded: bool) -> str:
    """The time block of a persona's prompt."""
    # The factual header: elapsed / left, phase, room for lines, and whether anyone concluded.
    head = (
        f"CLOCK: {_clock(timing.elapsed)} elapsed of {_clock(timing.duration)}; "
        f"{_clock(timing.remaining)} left. Phase: {timing.phase.replace('_', ' ')}. "
        f"Room for about {timing.lines_left} more lines from the whole panel. "
        f"Has someone already concluded the discussion? {'YES' if concluded else 'no'}.\n"
    )
    # The behavioural instruction depends on the phase.
    if timing.phase == OPENING:
        body = (
            "The discussion has just started. State your position with one clear reason. "
            "Do not summarise and do not try to conclude."
        )
    elif timing.phase == MIDDLE:
        # Pace the content: first half of the middle builds the case, the second tests it,
        # so the wrap-up has both sides to converge on.
        build = timing.elapsed < OPENING_SHARE * timing.duration + 0.5 * (
            timing.duration - timing.remaining_at_wrap_up - OPENING_SHARE * timing.duration
        )
        body = (
            "Plenty of time remains. Respond to the previous speaker and "
            + (
                "build the case: add a concrete reason, example or piece of evidence."
                if build
                else "test the case: raise a counterpoint, a trade-off or a risk, or answer one."
            )
            + " Do not summarise the whole discussion or try to conclude yet."
        )
    elif timing.phase == WRAP_UP:
        # Converging: only one persona should conclude, and others stay brief or silent.
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
        # Last seconds: a single closing line, and only if nobody has already concluded.
        body = "Almost out of time. " + (
            "Someone has already concluded: set speak to false."
            if concluded
            else "Nobody has concluded: give ONE closing sentence (at most 20 words) and set "
            "is_conclusion to true."
        )
    return head + body
