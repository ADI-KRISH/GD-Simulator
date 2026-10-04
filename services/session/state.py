"""Explicit state machines with transition tables (design §8, §9, §45).

Every transition goes through `transition()`. Invalid moves raise rather than
silently mutating — claude.md requires "explicit enum + transition table; reject
invalid transitions".
"""

from enum import StrEnum

from services.common.errors import InvalidStateTransition


class SessionState(StrEnum):
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
    INVITED = "INVITED"
    JOINED = "JOINED"
    READY = "READY"
    ACTIVE = "ACTIVE"
    DISCONNECTED = "DISCONNECTED"
    RECONNECTED = "RECONNECTED"
    LEFT = "LEFT"


class EvaluationState(StrEnum):
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
    if failure_state is not None and target is failure_state and current is not failure_state:
        # "ANY STATE → FAILED" (design §8).
        return target
    allowed = table.get(current, set())
    if target not in allowed:
        raise InvalidStateTransition(
            f"{machine}: {current} → {target} is not a legal transition "
            f"(allowed: {', '.join(sorted(str(s) for s in allowed)) or 'none'})"
        )
    return target


def transition_session(current: SessionState, target: SessionState) -> SessionState:
    return _check(
        SESSION_TRANSITIONS,
        current,
        target,
        machine="session",
        failure_state=SessionState.FAILED,
    )


def transition_participant(current: ParticipantState, target: ParticipantState) -> ParticipantState:
    return _check(PARTICIPANT_TRANSITIONS, current, target, machine="participant")


def transition_evaluation(current: EvaluationState, target: EvaluationState) -> EvaluationState:
    return _check(
        EVALUATION_TRANSITIONS,
        current,
        target,
        machine="evaluation",
        failure_state=EvaluationState.FAILED,
    )


def can_transition_session(current: SessionState, target: SessionState) -> bool:
    try:
        transition_session(current, target)
    except InvalidStateTransition:
        return False
    return True
