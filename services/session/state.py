"""Explicit state machines with transition tables (design §8, §9, §45).

Every transition goes through `transition()`. Invalid moves raise rather than
silently mutating — claude.md requires "explicit enum + transition table; reject
invalid transitions".

Purpose: Defines the three state machines (session, participant, evaluation job) as enums plus
    transition tables, and the functions that validate a move.
Why needed: claude.md requires explicit state machines that reject invalid transitions;
    centralising them stops any code path from setting an arbitrary state (for example ENDED to
    LIVE) and corrupting a session.
Contains:
  - SessionState / ParticipantState / EvaluationState: the legal states of each machine.
  - SESSION_TRANSITIONS / PARTICIPANT_TRANSITIONS / EVALUATION_TRANSITIONS: which state may
    follow which.
  - SESSION_TERMINAL: states that end the live phase, used by the timer and websocket layers.
  - _check: shared validator that returns the target state or raises InvalidStateTransition.
  - transition_session / transition_participant / transition_evaluation: public validators per
    machine.
  - can_transition_session: yes/no version of the session check without an exception.
Used by: services/evaluation/models.py, services/evaluation/router.py,
    services/evaluation/service.py, services/evaluation/worker.py, services/observer/service.py,
    services/practice/engine.py, services/realtime/timer.py, services/realtime/ws.py,
    services/session/models.py, services/session/repository.py, services/session/router.py,
    services/session/schemas.py, services/session/service.py, services/simulation.py,
    services/speech/router.py, services/tests/test_evaluation_api.py,
    services/tests/test_evaluation_pipeline.py, services/tests/test_notifications.py,
    services/tests/test_session_lifecycle.py, services/tests/test_state_machines.py
Depends on: services/common/errors.py
"""

from enum import StrEnum  # string-valued states, stored as text in the database

from services.common.errors import InvalidStateTransition  # raised on an illegal move


class SessionState(StrEnum):
    """Lifecycle of a whole GD session, from creation to a finished report."""

    CREATED = "CREATED"
    WAITING = "WAITING"
    READY = "READY"
    STARTING = "STARTING"
    LIVE = "LIVE"
    ENDING = "ENDING"
    ENDED = "ENDED"
    EVALUATING = "EVALUATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    RETRYING = "RETRYING"


class ParticipantState(StrEnum):
    """Where one participant is: invited, in the lobby, live, dropped or gone."""

    INVITED = "INVITED"
    JOINED = "JOINED"
    READY = "READY"
    ACTIVE = "ACTIVE"
    DISCONNECTED = "DISCONNECTED"
    RECONNECTED = "RECONNECTED"
    LEFT = "LEFT"


class EvaluationState(StrEnum):
    """Progress of one evaluation job through the AI pipeline."""

    QUEUED = "QUEUED"
    PREPARING = "PREPARING"
    RUNNING = "RUNNING"
    WAITING_FOR_AGENT = "WAITING_FOR_AGENT"
    AGGREGATING = "AGGREGATING"
    REPORT_GENERATING = "REPORT_GENERATING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


# Any session state may fail; FAILED recovers via RETRYING → EVALUATING.
SESSION_TRANSITIONS: dict[SessionState, set[SessionState]] = {
    SessionState.CREATED: {SessionState.WAITING},
    # WAITING ⇄ READY: readiness is revocable until STARTING latches the session.
    SessionState.WAITING: {SessionState.READY},
    SessionState.READY: {SessionState.STARTING, SessionState.WAITING},
    SessionState.STARTING: {SessionState.LIVE},
    SessionState.LIVE: {SessionState.ENDING},
    SessionState.ENDING: {SessionState.ENDED},
    SessionState.ENDED: {SessionState.EVALUATING},
    SessionState.EVALUATING: {SessionState.COMPLETED},
    SessionState.COMPLETED: set(),
    SessionState.FAILED: {SessionState.RETRYING},
    SessionState.RETRYING: {SessionState.EVALUATING},
}

# Participant lifecycle; a disconnect never ends the session (claude.md), it only moves to
# DISCONNECTED.
PARTICIPANT_TRANSITIONS: dict[ParticipantState, set[ParticipantState]] = {
    ParticipantState.INVITED: {ParticipantState.JOINED, ParticipantState.LEFT},
    ParticipantState.JOINED: {
        ParticipantState.READY,
        ParticipantState.DISCONNECTED,
        ParticipantState.LEFT,
    },
    # READY → JOINED is "un-ready" (FR-07 NOT_READY).
    ParticipantState.READY: {
        ParticipantState.ACTIVE,
        ParticipantState.JOINED,
        ParticipantState.DISCONNECTED,
        ParticipantState.LEFT,
    },
    ParticipantState.ACTIVE: {ParticipantState.DISCONNECTED, ParticipantState.LEFT},
    ParticipantState.DISCONNECTED: {ParticipantState.RECONNECTED, ParticipantState.LEFT},
    ParticipantState.RECONNECTED: {
        ParticipantState.ACTIVE,
        ParticipantState.DISCONNECTED,
        ParticipantState.LEFT,
    },
    ParticipantState.LEFT: set(),
}

# Evaluation job lifecycle (design §45) as seen by the worker.
EVALUATION_TRANSITIONS: dict[EvaluationState, set[EvaluationState]] = {
    EvaluationState.QUEUED: {EvaluationState.PREPARING},
    EvaluationState.PREPARING: {EvaluationState.RUNNING},
    EvaluationState.RUNNING: {EvaluationState.WAITING_FOR_AGENT, EvaluationState.AGGREGATING},
    EvaluationState.WAITING_FOR_AGENT: {EvaluationState.AGGREGATING, EvaluationState.RUNNING},
    EvaluationState.AGGREGATING: {EvaluationState.REPORT_GENERATING},
    EvaluationState.REPORT_GENERATING: {EvaluationState.COMPLETED},
    EvaluationState.COMPLETED: set(),
    # A failed job is retried by enqueuing a new attempt, which starts at QUEUED.
    EvaluationState.FAILED: {EvaluationState.QUEUED},
}

# States that terminate a session's live phase; used by the timer and ws layers.
SESSION_TERMINAL = {SessionState.COMPLETED, SessionState.FAILED}


def _check(table: dict, current, target, *, machine: str, failure_state=None):
    """Return `target` if `current -> target` is allowed by `table`, else raise.

    `failure_state`, when given, is reachable from every other state.
    """
    # Failure is the one universal edge: any state may drop into the machine's failure state.
    if failure_state is not None and target is failure_state and current is not failure_state:
        # "ANY STATE → FAILED" (design §8).
        return target
    # Normal case: look up what the current state is allowed to move to.
    allowed = table.get(current, set())
    # Reject illegal moves loudly so a bad state never reaches the database.
    if target not in allowed:
        raise InvalidStateTransition(
            f"{machine}: {current} → {target} is not a legal transition "
            f"(allowed: {', '.join(sorted(str(s) for s in allowed)) or 'none'})"
        )
    return target


def transition_session(current: SessionState, target: SessionState) -> SessionState:
    """Validate a session state change and return the new state."""
    # Session machine: any state may go to FAILED, otherwise follow SESSION_TRANSITIONS.
    return _check(
        SESSION_TRANSITIONS,
        current,
        target,
        machine="session",
        failure_state=SessionState.FAILED,
    )


def transition_participant(current: ParticipantState, target: ParticipantState) -> ParticipantState:
    """Validate a participant state change and return the new state."""
    # Participant machine has no failure state, only its table.
    return _check(PARTICIPANT_TRANSITIONS, current, target, machine="participant")


def transition_evaluation(current: EvaluationState, target: EvaluationState) -> EvaluationState:
    """Validate an evaluation-job state change and return the new state."""
    # Evaluation machine: any step may fail, and FAILED can restart at QUEUED.
    return _check(
        EVALUATION_TRANSITIONS,
        current,
        target,
        machine="evaluation",
        failure_state=EvaluationState.FAILED,
    )


def can_transition_session(current: SessionState, target: SessionState) -> bool:
    """True if the session may move from `current` to `target` (no exception)."""
    # Reuse the validator and turn its exception into a plain yes/no.
    try:
        transition_session(current, target)
    except InvalidStateTransition:
        return False
    return True
