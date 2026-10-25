"""The solo-practice conversation loop.

Purpose:     Runs the live conversation between one student and the AI personas:
             hears the student (speech or typing), decides which persona answers,
             generates the line, speaks it, and records every turn exactly like a
             group session would.
Why needed:  This is the heart of practice mode. It makes the personas behave like
             a real panel (take turns, open, invite a silent student, wrap up on
             time, never talk over the student) while feeding the normal speech,
             metrics and evaluation pipeline so a practice run gets a real report.
Contains:
  - Tuning constants (timeouts, token budget, chain probability, minimum words).
  - `LINE_SCHEMA`, `Line`   the JSON a persona must return and its parsed form.
  - `SendJson`, `SendBytes` callback types for sending to the browser.
  - `PracticeRoom`          one per connected student:
      - setup/teardown:   `load`, `start_session`, `close`
      - clock helpers:    `_timing`, `_live_clock`, `_now`
      - student's turn:   `on_utterance_start/end`, `on_audio`, `submit_text`,
                          `on_text`, `on_raise_hand`, `_take_student_turn`,
                          `on_playback_ended`
      - recording:        `_record`, `_announce`
      - persona's turn:   `_addressed`, `_note_turn`, `_choose`, `_start_burst`,
                          `_listen`, `_burst`, `_idle_loop`, `_generate`,
                          `_persona_turn`
Used by:     practice/router.py (creates a `PracticeRoom` per socket),
             tests/test_panel.py, tests/test_practice.py.
Depends on:  practice/panel.py (decisions), practice/timing.py (phases, imported
             as `clock`), llm/ (persona model), speech/ (STT, TTS, transcript),
             session/ (record speech and turns), realtime/ (hub, protocol, timer,
             ws), ai_platform/bridge.py (`UNNAMED_STUDENT`).

One `PracticeRoom` per connected student. It owns the turn-taking between the
student and the AI personas:

* The browser detects the student's speech (VAD), tells us when an utterance
  starts and ends, and sends the audio. We transcribe it, store it as a normal
  speech event + transcript segment + tracked turn, and let the personas answer.
* A persona's turn is LLM text -> TTS audio streamed to the browser. If the
  student starts talking while a persona is still speaking, that is a barge-in:
  playback is cut, and the student's turn is recorded as an interruption of that
  persona - tracked, and penalised in the score, exactly as in a group session.

Everything it writes (speech events, transcript segments, turns) goes through the
same service functions the multiplayer path uses, so the observer, metrics and
the whole evaluation pipeline run on a practice session unmodified.

ponytail: one in-process loop per socket, personas never interrupt the student.
Add persona barge-in when a persona's `eagerness` should override waiting.
"""

import asyncio  # tasks, events and sleeps that drive turn-taking
import logging  # warn on STT/LLM/TTS failures
import random  # randomised chaining and floor bids
import re  # count words in an utterance
import time  # monotonic clock for idle/silence tracking
import uuid  # session id type
from collections.abc import Awaitable, Callable  # callback types for sending to the browser
from dataclasses import dataclass  # the Line container

from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker  # DB session types

from services.ai_platform.bridge import UNNAMED_STUDENT  # placeholder name for unnamed students
from services.llm import LLMError, get_persona_llm_provider  # model that voices the personas
from services.practice import panel  # pure decisions: stances, bids, repeats, prompts
from services.practice import timing as clock  # clock phases and time guidance
from services.realtime.hub import hub  # broadcast to the room
from services.realtime.protocol import ServerEvent, server_frame  # WebSocket event frames
from services.realtime.timer import announce_discussion  # tell the room the discussion began
from services.realtime.ws import broadcast_turns  # announce tracked turns
from services.session import repository as session_repo  # load the session
from services.session import service as session_service  # record speech/turns, clocks
from services.session.models import GDSession  # session row type
from services.session.state import SessionState  # LIVE checks
from services.speech import service as speech_service  # persist transcript segments
from services.speech.stt import get_stt_provider  # transcribe the student's audio
from services.speech.tts import (  # speak persona lines
    TTSError,
    estimate_seconds,
    get_tts_provider,
)

logger = logging.getLogger(__name__)

# The student is always candidate "A" in a practice session.
STUDENT_LABEL = "A"
# A persona's audio is still audible for a moment after the student cuts in
# (the browser needs time to stop playback). Counting that tail is what makes a
# barge-in register as a genuine overlap - tune it to the client's stop latency.
BARGE_IN_TAIL_SECONDS = 0.6
# Silence after which a persona prompts the student.
IDLE_SECONDS = 14.0
# A student silent this long is invited in by name by the moderator (EC-14.1).
STUDENT_SILENT_SECONDS = 40.0
# Token budget for one spoken line: the words are capped by the prompt; a reasoning
# model needs headroom above them or its JSON is cut off.
PERSONA_MAX_TOKENS = 400
# How long a reply waits (in 0.1 s steps) for a line the student cut to unwind.
CUT_LINE_WAIT_STEPS = 50
# A typed turn (FR-14.19): its longest accepted length, and how long it waits (in
# 0.1 s steps) for a persona's line to finish — typing never cuts anyone off.
MAX_TYPED_CHARS = 600
TYPED_WAIT_STEPS = 300
# Lines of the discussion each persona prompt carries. A persona line costs ~1.1K
# tokens on Groq (measured 2026-10-19) against a free tier's 8K tokens a minute
# per model; ten lines instead of sixteen saves ~15% with no loss of context.
HISTORY_LINES = 10
# Near the end the table should not sit in silence waiting for a conclusion.
WRAP_UP_IDLE_SECONDS = 6.0
# After a persona speaks, chance that another persona follows on (max chain below).
CHAIN_PROBABILITY = 0.35
MAX_CHAIN = 2
OPENING_DELAY_SECONDS = 2.5
# After the student's words are transcribed, wait this long (and until nobody is
# mid-sentence or being transcribed) before a persona answers: hear them out first.
LISTEN_HOLD_SECONDS = 0.6
LISTEN_WAIT_LIMIT_SECONDS = 4.0
# Extra time allowed for the browser to finish playing a line before we give up
# waiting for its PLAYBACK_ENDED message.
PLAYBACK_SLACK_SECONDS = 4.0
# Utterances shorter than this are mic noise, not contributions.
MIN_WORDS = 2

# JSON shape a persona's LLM reply must follow.
LINE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["speak", "is_conclusion", "text"],
    "properties": {
        # False = stay silent (e.g. the discussion was already concluded).
        "speak": {"type": "boolean"},
        # True = this line wraps the whole discussion up.
        "is_conclusion": {"type": "boolean"},
        "text": {"type": "string"},
    },
}


@dataclass(frozen=True, slots=True)
class Line:
    """One generated persona line: whether to speak, what to say, and if it concludes."""

    speak: bool
    text: str
    is_conclusion: bool


# Async callbacks the websocket layer hands the room for sending JSON / audio bytes.
SendJson = Callable[[dict], Awaitable[None]]
SendBytes = Callable[[bytes], Awaitable[None]]


class PracticeRoom:
    """The live conversation between one student and the AI personas."""

    def __init__(
        self,
        factory: async_sessionmaker[AsyncSession],
        session_id: uuid.UUID,
        student_name: str,
        send_json: SendJson,
        send_bytes: SendBytes,
    ) -> None:
        # Identity and database access for this room.
        self.factory = factory
        self.session_id = session_id
        self.sid = str(session_id)
        # Captions show what the account says; the prompts need a name the panel
        # can say. A platform student with none is "Student" on screen, but that
        # is a role: told to invite "Student" by name, the moderator invited
        # himself (19 Oct). So they are "the student", addressed as "you".
        self.student_display = student_name
        self.student_named = student_name.strip() not in ("", UNNAMED_STUDENT)
        self.student_name = student_name if self.student_named else "the student"
        self._send_json = send_json
        self._send_bytes = send_bytes

        # Live conversation state: who is speaking, what has been said, who was cut off,
        # and the student's current utterance.
        self.personas: dict[str, dict] = {}
        self.lines: list[tuple[str, str]] = []  # (speaker name, text)
        self.last_speaker: str | None = None
        self.last_activity = time.monotonic()

        self._started_at_gd: GDSession | None = None  # for the discussion clock
        self.speaking: str | None = None  # persona whose line is being generated/streamed
        self.playing: str | None = None  # persona whose audio is audible right now
        self.barge = asyncio.Event()
        self.cut_label: str | None = None  # persona the student last cut off
        self.user_started: float | None = None
        self.user_ended: float | None = None
        self.user_active = False
        self.burst: asyncio.Task | None = None
        self.idle: asyncio.Task | None = None
        self.opened = False
        self.stt_pending = 0  # utterances still being transcribed
        self.playback_done = asyncio.Event()  # the browser finished playing the line
        # Someone (student or persona) has already wrapped the discussion up.
        self.concluded = False

        # The panel's bookkeeping (panel.py): who runs the room, whose turn it is,
        # how much of the talk each voice has held, and any turn owed next.
        self.moderator: str | None = None
        self.turn = 0
        self.last_turn_of: dict[str, int] = {}
        self.floor: dict[str, float] = {}
        self.last_text = ""
        self.student_spoke_at = time.monotonic()
        self.redirects = 0
        self.yields = 0  # repeats in a row; three bring a curveball (EC-14.4)
        self.forced: tuple[str, str | None, str] | None = None  # (label, cue, target label)
        self.ai_question = ""  # the student just asked whether we are an AI
        self.rng = random.Random()
        self.last_end = 0.0  # when the latest recorded turn ended
        self.hands = 0  # raised hands (EC-14.10: a distinct entry style)
        self._tasks: set[asyncio.Task] = set()  # typed turns in flight
        self.time_checks: set[str] = set()  # moderator time checks already given

    # ------------------------------------------------------------------ setup

    async def load(self) -> GDSession:
        """Load the session's personas from the database and pick the moderator."""
        async with self.factory() as db:
            gd = await session_service.require_session(db, self.session_id)
            # Index personas by label (P1, P2...) and choose who also moderates.
            self.personas = {p["label"]: p for p in gd.personas}
            self.moderator = panel.pick_moderator(self.personas)
            self._started_at_gd = gd
            return gd

    async def start_session(self, user) -> None:
        """Student pressed start: ready up and begin preparation."""
        async with self.factory() as db:
            gd = await session_service.require_session(db, self.session_id)
            # Only a not-yet-started session needs readying and starting.
            if SessionState(gd.state) in {SessionState.WAITING, SessionState.READY}:
                await session_service.set_ready(db, gd, user, True)
                await session_service.start_session(db, gd)
                await db.commit()
        if SessionState(gd.state) is SessionState.LIVE:
            # No preparation (or a reconnect into a running session): the ticker never
            # sees the move to LIVE, so the room is told here — or the page stayed on
            # "Preparation", its composer disabled and its microphone deaf.
            await announce_discussion(gd)
        else:
            await hub.broadcast(
                self.sid,
                server_frame(ServerEvent.SESSION_STARTED, self.sid, {"phase": "preparation"}),
            )
        # Start the background loop that opens the discussion and nudges a quiet student.
        if self.idle is None:
            self.idle = asyncio.create_task(self._idle_loop(), name=f"practice-idle-{self.sid}")

    async def close(self) -> None:
        """Cancel every background task this room started (called on disconnect)."""
        tasks = [t for t in (self.burst, self.idle, *self._tasks) if t and not t.done()]
        for task in tasks:
            task.cancel()
        # Wait for the cancellations to finish so nothing keeps running after disconnect.
        await asyncio.gather(*tasks, return_exceptions=True)

    # ------------------------------------------------------------------ clock

    async def _timing(self) -> clock.Timing | None:
        """Where the discussion is on the clock, or None unless it is LIVE."""
        async with self.factory() as db:
            gd = await session_repo.get_session_by_id(db, self.session_id)
            # Outside LIVE (preparing, ended...) the personas shouldn't be talking.
            if gd is None or SessionState(gd.state) is not SessionState.LIVE:
                return None
            self._started_at_gd = gd
            return clock.timing_for(
                session_service.elapsed_discussion_seconds(gd), float(gd.duration_seconds)
            )

    async def _live_clock(self) -> float | None:
        """Seconds into the discussion, or None unless the session is LIVE."""
        async with self.factory() as db:
            gd = await session_repo.get_session_by_id(db, self.session_id)
            if gd is None or SessionState(gd.state) is not SessionState.LIVE:
                return None
            self._started_at_gd = gd
            return session_service.elapsed_discussion_seconds(gd)

    def _now(self) -> float:
        """Seconds into the discussion, from the last session row we loaded (0 if none)."""
        gd =self._started_at_gd
        return session_service.elapsed_discussion_seconds(gd) if gd else 0.0

    # --------------------------------------------------------- student's turn

    async def on_utterance_start(self) -> None:
        """The browser heard the student start talking; cut off any persona (barge-in)."""
        # Ignore speech outside the live discussion window.
        if await self._live_clock() is None:
            return
        self.user_active = True
        self.last_activity = time.monotonic()
        self.user_started = self._now()
        if self.speaking:
            # Barge-in. It only counts as an interruption if the persona was audible.
            self.cut_label = self.playing
            self.barge.set()
            await self._send_json(server_frame(ServerEvent.STOP_AUDIO, self.sid, {}))
        # Otherwise cancel any persona reply still being prepared: the student has the floor.
        elif self.burst and not self.burst.done():
            self.burst.cancel()
        # Tell the room the student started speaking (drives the live indicator).
        await hub.broadcast(
            self.sid,
            server_frame(
                ServerEvent.SPEECH_STARTED,
                self.sid,
                {"candidate_label": STUDENT_LABEL, "start": self.user_started},
            ),
        )

    async def on_utterance_end(self) -> None:
        """The browser heard the student stop; remember when (the audio follows)."""
        if self.user_started is not None:
            self.user_ended = max(self._now(), self.user_started)

    async def on_audio(self, blob: bytes) -> None:
        """The utterance's audio arrived: transcribe, record, let the personas answer."""
        # Take the utterance's timing and any barge-in marker, then reset for the next one.
        start, end = self.user_started, self.user_ended
        cut, self.cut_label = self.cut_label, None
        self.user_started = self.user_ended = None
        self.user_active = False
        await hub.broadcast(
            self.sid,
            server_frame(
                ServerEvent.SPEECH_STOPPED,
                self.sid,
                {"candidate_label": STUDENT_LABEL, "start": start, "end": end},
            ),
        )
        # No timing or no audio: nothing to transcribe.
        if start is None or end is None or not blob:
            return

        # While transcribing, personas hold their reply (stt_pending > 0).
        self.stt_pending += 1
        try:
            transcription = await get_stt_provider().transcribe(blob)
        except Exception as exc:  # STT vendor/runtime failure must not kill the room
            logger.warning("practice STT failed", extra={"error": str(exc)})
            await self._send_json(
                server_frame(
                    ServerEvent.ERROR, self.sid, {"code": "stt_failed", "message": str(exc)}
                )
            )
            return
        finally:
            self.stt_pending -= 1
        text = transcription.text.strip()
        # Fewer than MIN_WORDS words is mic noise, not a contribution.
        if len(re.findall(r"\w+", text)) < MIN_WORDS:
            return
        await self._take_student_turn(start, end, text, cut)

    def submit_text(self, text: str) -> None:
        """Queue a typed turn without holding up the socket (it may wait for a line)."""
        # Run as a background task; keep a reference so it isn't garbage-collected,
        # and drop it automatically when it finishes.
        task = asyncio.create_task(self.on_text(text), name=f"practice-text-{self.sid}")
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def on_text(self, text: str) -> None:
        """A typed turn (FR-14.19): the spoken path, minus speech-to-text.

        Typing never cuts a persona off — a line being said finishes first. Then
        the turn holds the floor for as long as it would take to say, and the
        panel waits that out before answering, as it would for a spoken turn. It
        used to be squeezed into the instant before "now": three typed points
        came to two seconds of speaking time, and the scoring reads speaking time.
        """
        # Tidy whitespace and cap the length; ignore too-short text or text outside LIVE.
        text = " ".join(text.split())[:MAX_TYPED_CHARS]
        if len(re.findall(r"\w+", text)) < MIN_WORDS or await self._live_clock() is None:
            return
        # Let any persona line in progress finish before the typed turn begins.
        for _ in range(TYPED_WAIT_STEPS):
            if not self.speaking:
                break
            await asyncio.sleep(0.1)
        # Give the typed turn a realistic speaking duration, starting after the last turn.
        now = self._now()
        start = max(now, self.last_end)
        end = start + estimate_seconds(text)
        await self._take_student_turn(
            start, end, text, None, hold=LISTEN_HOLD_SECONDS + (end - now)
        )

    async def on_raise_hand(self) -> None:
        """FR-14.12: a raised hand; the moderator invites the student at the next gap."""
        if not self.moderator or await self._live_clock() is None:
            return
        self.hands += 1
        # Queue an invitation from the moderator; it is delivered at the next gap.
        self.forced = (self.moderator, "hand", "")
        # If the room is quiet right now, deliver it immediately.
        busy = self.speaking or self.user_active or (self.burst and not self.burst.done())
        if not busy:
            self._start_burst()

    async def _take_student_turn(
        self, start: float, end: float, text: str, cut: str | None, hold: float | None = None
    ) -> None:
        """Record what the student said — spoken or typed — and let the panel answer.

        `hold`: how long the panel waits before answering (a typed turn's spoken
        length); by default the short "hear them out" pause.
        """
        # If the student wraps the discussion up near the end, the panel won't conclude again.
        when = await self._timing()
        if when and when.converging and clock.looks_like_conclusion(text):
            self.concluded = True
        # Persist the turn (speech event, tracked turn, transcript segment).
        changed, interrupts = await self._record(STUDENT_LABEL, start, end, text, interrupted=cut)
        # Update the conversation bookkeeping used by the next persona's prompt and bid.
        self.lines.append((self.student_name, text))
        self.last_speaker = STUDENT_LABEL
        self.last_activity = time.monotonic()
        self._note_turn(STUDENT_LABEL, start, end, text)
        self.student_spoke_at = time.monotonic()
        # Remember if they asked "are you an AI?" so the reply admits it (AC-14.5).
        self.ai_question = text if panel.asked_if_ai(text) else ""
        if self.forced and self.forced[1] == "hand":
            # They came in on their own: inviting them now would come after the fact.
            self.forced = None
        # EC-14.2: the student holds most of the talk; the moderator hands it on.
        if self.moderator and panel.should_redirect(self.floor, self.redirects):
            target = panel.quietest(self.personas, self.floor, exclude=self.moderator)
            if target:
                self.redirects += 1
                self.forced = (self.moderator, "redirect", target)
        # Show the line in the transcript, echo it back to the student, then let personas reply.
        await self._announce(STUDENT_LABEL, self.student_display, text, changed, interrupts)
        await self._send_json(server_frame(ServerEvent.USER_TRANSCRIPT, self.sid, {"text": text}))
        self._start_burst(
            addressed=self._addressed(text),
            hold=LISTEN_HOLD_SECONDS if hold is None else hold,
        )

    def on_playback_ended(self) -> None:
        """The browser finished playing (or gave up on) the persona's line."""
        self.playback_done.set()

    # ------------------------------------------------------------- recording

    async def _record(
        self, label: str, start: float, end: float, text: str, *, interrupted: str | None = None
    ):
        """Save one turn as a speech event, tracked turn and transcript segment."""
        async with self.factory() as db:
            gd = await session_service.require_session(db, self.session_id)
            # Same call group sessions use, so metrics and turns stay consistent.
            _, changed = await session_service.record_speech_tracked(
                db, gd, label, start, end, interrupted=interrupted
            )
            # Save the words immediately (the transcript is the source of truth).
            await speech_service.persist_segment(db, gd.id, label, start, end, text, 0.95)
            interrupts = await session_service.interruption_times(db, self.session_id)
            await db.commit()
        return changed, interrupts

    async def _announce(self, label, name, text, changed, interrupts) -> None:
        """Tell the room a line was added to the transcript, plus any turn changes."""
        await hub.broadcast(
            self.sid,
            server_frame(
                ServerEvent.TRANSCRIPT_APPENDED,
                self.sid,
                {"candidate_label": label, "name": name, "text": text, "segments": 1},
            ),
        )
        await broadcast_turns(self.sid, changed, interrupts)

    # --------------------------------------------------------- persona's turn

    def _addressed(self, text: str) -> str | None:
        """Label of the persona the text names ("Arjun, what do you think?"), if any."""
        lowered = text.lower()
        # Whole-word, case-insensitive match on each persona's name.
        for label, persona in self.personas.items():
            if re.search(rf"\b{re.escape(persona['name'].lower())}\b", lowered):
                return label
        return None

    def _note_turn(self, label: str, start: float, end: float, text: str) -> None:
        """Bookkeeping for the bids: whose turn it was, and how long they held the floor."""
        # Advance the turn counter and credit this speaker with the time they held the floor.
        self.turn += 1
        self.last_turn_of[label] = self.turn
        self.floor[label] = self.floor.get(label, 0.0) + max(0.0, end - start)
        self.last_text = text
        self.last_end = max(self.last_end, end)

    def _choose(
        self, addressed: str | None = None, converging: bool = False
    ) -> tuple[str | None, str | None, str]:
        """Who speaks next, and any special task for that turn: (label, cue, target).

        A turn owed (a redirect, a curveball, the persona a redirect named) comes
        first; then whoever the last speaker named; then the highest bid (FR-14.6).
        """
        if not self.personas:
            return None, None, ""
        # 1) A turn already owed (redirect, curveball, raised hand) goes first.
        if self.forced:
            forced, self.forced = self.forced, None
            return forced
        # 2) Whoever the last speaker addressed by name.
        if addressed and addressed != self.last_speaker:
            return addressed, None, ""
        # 3) Otherwise the highest bid; near the end the moderator is pushed to close.
        closing = converging and not self.concluded
        label = panel.choose_next(
            self.personas,
            last_speaker=self.last_speaker,
            last_text=self.last_text,
            turn=self.turn,
            last_turn_of=self.last_turn_of,
            floor=self.floor,
            moderator=self.moderator,
            closing=closing,
            rng=self.rng,
        )
        return label, ("close" if closing and label == self.moderator else None), ""

    def _start_burst(
        self, addressed: str | None = None, hold: float = 0.0, opening: bool = False
    ) -> None:
        """Start (or restart) the background task in which personas respond."""
        if self.speaking and not self.barge.is_set():
            return  # one voice at a time: never start a second line over a live one
        # A line the student has cut may still be unwinding (its audio stream has not
        # noticed yet). It must not swallow the reply to what they just said: that
        # left the student unanswered until the idle nudge, 14 s later. `_burst`
        # waits for the cut line to finish first.
        # Replace any pending reply with a fresh one, then start it in the background.
        if self.burst and not self.burst.done():
            self.burst.cancel()
        self.burst = asyncio.create_task(
            self._burst(addressed, hold, opening), name=f"practice-{self.sid}"
        )

    async def _listen(self, hold: float) -> bool:
        """Wait until the student is done talking. False if they are still going."""
        # A short pause to "hear them out", then wait (up to a limit) while they speak
        # or their audio is still being transcribed.
        await asyncio.sleep(hold)
        waited = 0.0
        while (self.user_active or self.stt_pending) and waited < LISTEN_WAIT_LIMIT_SECONDS:
            await asyncio.sleep(0.2)
            waited += 0.2
        return not self.user_active and not self.stt_pending

    async def _burst(
        self, addressed: str | None = None, hold: float = 0.0, opening: bool = False
    ) -> None:
        """A persona answers; sometimes another follows on, then the floor is the student's."""
        try:
            if not await self._listen(hold):
                return  # the student is still speaking; their next utterance re-triggers us
            # Wait for a line the student cut off to finish unwinding before replying.
            for _ in range(CUT_LINE_WAIT_STEPS):
                if not self.speaking:
                    break
                await asyncio.sleep(0.1)  # a line the student cut is still unwinding
            if self.speaking:
                return
            # Up to MAX_CHAIN personas speak in a row (usually one).
            for turn in range(MAX_CHAIN):
                when = await self._timing()
                if when is None or when.phase == clock.OVER:
                    return
                if self.concluded and when.converging:
                    return  # the discussion has been wrapped up: say nothing more
                if opening and turn == 0 and self.moderator:
                    # FR-14.3 / EC-14.1: the moderator opens and invites the student.
                    label, cue, target = self.moderator, "open", ""
                else:
                    label, cue, target = self._choose(
                        addressed if turn == 0 else None, when.converging
                    )
                if label is None or self.user_active:
                    return
                cut = await self._persona_turn(label, cue, target)
                if cue in ("open", "hand", "time") or cut or self.user_active or self.concluded:
                    return  # after an invitation the floor is the student's
                if self.forced:
                    continue  # a turn is owed (the persona a redirect named): take it now
                # Otherwise a follow-on speaker joins only some of the time (natural pacing).
                if random.random() > CHAIN_PROBABILITY:
                    return
                await asyncio.sleep(0.6)
        except asyncio.CancelledError:
            raise
        except Exception:
            # A persona failure must not take the socket down; log it and wait for the next trigger.
            logger.exception("persona burst failed", extra={"session_id": self.sid})

    async def _idle_loop(self) -> None:
        """Open the discussion, then nudge the student when the room goes quiet."""
        try:
            while True:
                await asyncio.sleep(1.0)
                # Not LIVE: either not started yet (keep waiting) or finished (stop the loop).
                if await self._live_clock() is None:
                    async with self.factory() as db:
                        gd = await session_repo.get_session_by_id(db, self.session_id)
                        if gd and SessionState(gd.state) in {
                            SessionState.ENDING,
                            SessionState.ENDED,
                            SessionState.EVALUATING,
                            SessionState.COMPLETED,
                            SessionState.FAILED,
                        }:
                            return
                    continue
                when = await self._timing()
                if when is None or when.phase == clock.OVER:
                    continue
                if when.converging and self.concluded:
                    continue
                # How long the room has been quiet, and how long to tolerate (shorter near the end).
                quiet = time.monotonic() - self.last_activity
                nudge_after = WRAP_UP_IDLE_SECONDS if when.converging else IDLE_SECONDS
                # Never start a reply while someone is mid-speech or a reply is underway.
                busy = (
                    self.speaking
                    or self.user_active
                    or self.stt_pending
                    or (self.burst and not self.burst.done())
                )
                if busy:
                    continue
                if self.opened and self.forced:
                    self._start_burst()  # a turn is owed (a raised hand): take it now
                    continue
                # The moderator keeps the room on the clock: halfway, and when to converge.
                check = clock.time_check_due(when, self.time_checks) if self.moderator else None
                if self.opened and check and not self.concluded:
                    self.time_checks.add(check)
                    self.forced = (self.moderator, "time", clock.remaining_phrase(when))
                    self._start_burst()
                    continue
                # First, the moderator opens the discussion after a short delay...
                if not self.opened and quiet >= OPENING_DELAY_SECONDS:
                    self.opened = True
                    self._start_burst(opening=True)
                # ...afterwards, a quiet room gets a nudge from a persona.
                elif self.opened and quiet >= nudge_after:
                    self.last_activity = time.monotonic()
                    silent = time.monotonic() - self.student_spoke_at
                    if self.moderator and not when.converging and silent >= STUDENT_SILENT_SECONDS:
                        # EC-14.1: a silent student is brought in by name, once per stretch.
                        self.forced = (self.moderator, "invite", "")
                        self.student_spoke_at = time.monotonic()
                    self._start_burst()
        except asyncio.CancelledError:
            raise

    async def _generate(
        self,
        persona: dict,
        when: clock.Timing,
        cue: str | None = None,
        target: str = "",
        repeated: str = "",
    ) -> Line:
        """Ask the persona LLM for this persona's next line, given the clock and recent talk."""
        # The other panel members' names, for the prompt.
        others = ", ".join(
            p["name"] for p in self.personas.values() if p["label"] != persona["label"]
        )
        # Only the most recent lines are sent, to stay within the model's token budget.
        history = (
            "\n".join(f"{who}: {text}" for who, text in self.lines[-HISTORY_LINES:])
            or "(nobody has spoken yet)"
        )
        async with self.factory() as db:
            gd = await session_service.require_session(db, self.session_id)
            topic = gd.topic
        # Assemble the persona's full system prompt (character, stance, rules, clock, task).
        system = panel.persona_system(
            persona,
            student=self.student_name,
            named=self.student_named,
            others=others,
            topic=topic,
            max_words=clock.word_budget(when),
            clock_guidance=clock.guidance(when, self.concluded),
            is_moderator=persona["label"] == self.moderator,
            cue=cue,
            target=self.personas[target]["name"] if target in self.personas else target,
            repeated=repeated,
            asked_ai=bool(self.ai_question),
        )
        result = await get_persona_llm_provider().complete_json(
            system=system,
            prompt=f"Discussion so far:\n{history}\n\nWhat do you say next?",
            schema=LINE_SCHEMA,
            # The prompt caps the words; this caps tokens. A reasoning model spends
            # some before it answers, and at 150 a long prompt left too few for the
            # JSON (Groq: json_validate_failed, a student's turn went unanswered).
            max_tokens=PERSONA_MAX_TOKENS,
        )
        # Clean the spoken text (some models wrap it in quotes) and build the typed result.
        text = str(result.data.get("text", "")).strip().strip('"')
        return Line(
            speak=bool(result.data.get("speak", True)) and bool(text),
            text=text,
            is_conclusion=bool(result.data.get("is_conclusion", False)),
        )

    async def _persona_turn(self, label: str, cue: str | None = None, target: str = "") -> bool:
        """One persona speaks. Returns True if the student cut them off.

        Two stages: the line is generated and streamed to the browser (`speaking`;
        nothing is audible yet), then the browser plays it (`playing`). Only
        audible speech is recorded and only cutting off audible speech counts as
        an interruption - talking over a persona who has not started yet does not.

        `cue` is a special task for this turn (panel.cue_text): opening, inviting the
        student, a redirect to `target`, a curveball, the close.
        """
        persona = self.personas[label]
        # Don't start a line if the discussion isn't live or has run out of time.
        when = await self._timing()
        if when is None or when.phase == clock.OVER:
            return False
        try:
            line = await self._generate(persona, when, cue, target)
            if line.speak and cue is None:
                # FR-14.5: a line that restates a recent point goes back once; restated
                # again, the persona yields, and three yields bring a curveball (EC-14.4).
                recent = [said for _, said in self.lines[-8:]]
                repeated = panel.repeats(line.text, recent)
                if repeated:
                    line = await self._generate(persona, when, "fresh", repeated=repeated)
                    if line.speak and panel.repeats(line.text, recent):
                        self.yields += 1
                        if self.yields >= panel.YIELDS_BEFORE_CURVEBALL and self.moderator:
                            self.yields = 0
                            self.forced = (self.moderator, "curveball", "")
                        return False
        except LLMError as exc:
            logger.warning("persona turn failed", extra={"session_id": self.sid, "error": str(exc)})
            return False
        # A persona may choose silence (e.g. someone already concluded); a conclusion
        # given after someone else's is dropped rather than repeated.
        if not line.speak or (line.is_conclusion and self.concluded):
            return False
        self.yields = 0
        # AC-14.5: asked whether it is an AI, the first reply says so — every time.
        text = panel.ensure_honest(line.text, self.ai_question)
        self.ai_question = ""
        # The student started talking while we generated: drop the line, don't talk over them.
        if self.user_active or await self._live_clock() is None:
            return False

        # Stage 1: announce the line and stream its audio (nothing audible yet).
        tts = get_tts_provider()
        self.speaking = label
        self.barge.clear()
        self.playback_done.clear()
        started_audio = False
        finished = False
        total_bytes = 0
        audio = tts.produces_audio
        try:
            # Tell the browser a line is coming (text, whether audio follows, voice gender).
            await self._send_json(
                server_frame(
                    ServerEvent.PERSONA_AUDIO_START,
                    self.sid,
                    {
                        "candidate_label": label,
                        "name": persona["name"],
                        "text": text,
                        "audio": audio,
                        "gender": persona.get("gender", "f"),
                    },
                )
            )
            started_audio = True
            # Stream the synthesized speech to the browser chunk by chunk.
            if audio:
                try:
                    async for chunk in tts.stream(text, persona["voice_id"]):
                        if self.barge.is_set():
                            return True  # cut before anything was audible
                        total_bytes += len(chunk)
                        await self._send_bytes(chunk)
                except TTSError as exc:
                    # TTS failed: fall back to the browser's built-in voice for this line.
                    logger.warning("tts failed, falling back", extra={"error": str(exc)})
                    audio = False
                    await self._send_json(
                        server_frame(ServerEvent.PERSONA_AUDIO_FALLBACK, self.sid, {"text": text})
                    )
            if self.barge.is_set():
                return True
            # How long the line will take to say (from audio size, or a words-per-second estimate).
            duration = total_bytes / tts.mp3_bytes_per_second if audio else estimate_seconds(text)

            # Stage 2: the browser starts playing on READY; from here the persona holds the floor.
            start = await self._live_clock()
            if start is None:
                return False
            self.playing = label
            started_audio = True
            await self._send_json(
                server_frame(ServerEvent.PERSONA_AUDIO_READY, self.sid, {"candidate_label": label})
            )
            await hub.broadcast(
                self.sid,
                server_frame(
                    ServerEvent.SPEECH_STARTED,
                    self.sid,
                    {"candidate_label": label, "start": start},
                ),
            )
            # Hold the floor until the browser says the line is done (or the student
            # cuts in). The byte-count estimate is only the fallback.
            waits = [
                asyncio.create_task(self.barge.wait()),
                asyncio.create_task(self.playback_done.wait()),
            ]
            await asyncio.wait(
                waits,
                timeout=max(duration * 1.3, duration + PLAYBACK_SLACK_SECONDS),
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in waits:
                task.cancel()
            # Work out when the persona really stopped: cut off, finished playing, or estimated.
            cut = self.barge.is_set()
            if cut:
                end = max(self._now(), start) + BARGE_IN_TAIL_SECONDS
            elif self.playback_done.is_set():
                end = max(self._now(), start)
            else:
                end = start + duration
            finished = True
            changed, interrupts = await self._record(label, start, end, text)
        finally:
            if started_audio and not finished:
                # Cancelled mid-line: make sure the browser does not keep talking.
                try:
                    await self._send_json(server_frame(ServerEvent.STOP_AUDIO, self.sid, {}))
                except Exception:  # noqa: BLE001 - the socket may already be gone
                    pass
            self.speaking = None
            self.playing = None

        # Update shared state: conclusion flag, history, last speaker and floor shares.
        if line.is_conclusion:
            self.concluded = True
        self.lines.append((persona["name"], text))
        self.last_speaker = label
        self.last_activity = time.monotonic()
        self._note_turn(label, start, end, text)
        if cue == "redirect" and target in self.personas and not cut:
            self.forced = (target, None, "")  # the persona just handed the floor answers
        # Close out the line for the browser, then log it in the transcript and turn tracking.
        await self._send_json(
            server_frame(
                ServerEvent.PERSONA_AUDIO_END, self.sid, {"candidate_label": label, "cut": cut}
            )
        )
        await hub.broadcast(
            self.sid,
            server_frame(
                ServerEvent.SPEECH_STOPPED,
                self.sid,
                {"candidate_label": label, "start": start, "end": end},
            ),
        )
        await self._announce(label, persona["name"], text, changed, interrupts)
        return cut
