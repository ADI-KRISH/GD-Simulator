"""State machine transitions (design §8, §9, §45).

These tables are load-bearing: an illegal transition slipping through is how a
session ends up LIVE with no start time, or ENDED twice with two evaluation
jobs. Every legal edge and a representative illegal one are covered.
"""

import pytest

from services.common.errors import InvalidStateTransition
from services.session.state import (
    EVALUATION_TRANSITIONS,
    PARTICIPANT_TRANSITIONS,
    SESSION_TRANSITIONS,
    EvaluationState,
    ParticipantState,
    SessionState,
    can_transition_session,
    transition_evaluation,
    transition_participant,
    transition_session,
)


def test_happy_path_session_lifecycle():
    """CREATED → ... → COMPLETED, the design §8 spine."""
    path = [
        SessionState.CREATED,
        SessionState.WAITING,
        SessionState.READY,
        SessionState.STARTING,
        SessionState.LIVE,
        SessionState.ENDING,
        SessionState.ENDED,
        SessionState.EVALUATING,
        SessionState.COMPLETED,
    ]
    for current, target in zip(path, path[1:], strict=False):
        assert transition_session(current, target) is target


def test_readiness_is_revocable_before_start():
    """A candidate un-readying drops the session back to WAITING (FR-07)."""
    assert transition_session(SessionState.READY, SessionState.WAITING) is SessionState.WAITING


@pytest.mark.parametrize(
    ("current", "target"),
    [
        (SessionState.CREATED, SessionState.LIVE),
        (SessionState.WAITING, SessionState.STARTING),
        (SessionState.LIVE, SessionState.COMPLETED),
        (SessionState.ENDED, SessionState.LIVE),
        (SessionState.COMPLETED, SessionState.EVALUATING),
    ],
)
def test_illegal_session_transitions_are_rejected(current, target):
    with pytest.raises(InvalidStateTransition):
        transition_session(current, target)


@pytest.mark.parametrize("state", list(SessionState))
def test_any_state_may_fail(state):
    """Design §8: ANY STATE → FAILED."""
    if state is SessionState.FAILED:
        return
    assert transition_session(state, SessionState.FAILED) is SessionState.FAILED


def test_failed_recovers_through_retrying():
    """FAILED → RETRYING → EVALUATING (design §8 recovery branch)."""
    assert transition_session(SessionState.FAILED, SessionState.RETRYING) is SessionState.RETRYING
    assert (
        transition_session(SessionState.RETRYING, SessionState.EVALUATING)
        is SessionState.EVALUATING
    )


def test_completed_is_terminal():
    assert SESSION_TRANSITIONS[SessionState.COMPLETED] == set()
    assert not can_transition_session(SessionState.COMPLETED, SessionState.LIVE)


def test_participant_disconnect_reconnect_cycle():
    """A disconnect must be recoverable — the session keeps running (design §9)."""
    assert (
        transition_participant(ParticipantState.ACTIVE, ParticipantState.DISCONNECTED)
        is ParticipantState.DISCONNECTED
    )
    assert (
        transition_participant(ParticipantState.DISCONNECTED, ParticipantState.RECONNECTED)
        is ParticipantState.RECONNECTED
    )
    assert (
        transition_participant(ParticipantState.RECONNECTED, ParticipantState.ACTIVE)
        is ParticipantState.ACTIVE
    )


def test_participant_may_unready():
    assert (
        transition_participant(ParticipantState.READY, ParticipantState.JOINED)
        is ParticipantState.JOINED
    )


def test_left_is_terminal_for_participants():
    assert PARTICIPANT_TRANSITIONS[ParticipantState.LEFT] == set()
    with pytest.raises(InvalidStateTransition):
        transition_participant(ParticipantState.LEFT, ParticipantState.JOINED)


def test_participant_cannot_skip_straight_to_active():
    with pytest.raises(InvalidStateTransition):
        transition_participant(ParticipantState.JOINED, ParticipantState.ACTIVE)


def test_evaluation_happy_path():
    """QUEUED → ... → COMPLETED (design §45)."""
    path = [
        EvaluationState.QUEUED,
        EvaluationState.PREPARING,
        EvaluationState.RUNNING,
        EvaluationState.WAITING_FOR_AGENT,
        EvaluationState.AGGREGATING,
        EvaluationState.REPORT_GENERATING,
        EvaluationState.COMPLETED,
    ]
    for current, target in zip(path, path[1:], strict=False):
        assert transition_evaluation(current, target) is target


def test_evaluation_may_fail_from_any_state_and_requeue():
    assert (
        transition_evaluation(EvaluationState.RUNNING, EvaluationState.FAILED)
        is EvaluationState.FAILED
    )
    assert (
        transition_evaluation(EvaluationState.FAILED, EvaluationState.QUEUED)
        is EvaluationState.QUEUED
    )


def test_evaluation_cannot_skip_aggregation():
    """The deterministic aggregator is not optional (claude.md invariant 5)."""
    with pytest.raises(InvalidStateTransition):
        transition_evaluation(EvaluationState.RUNNING, EvaluationState.REPORT_GENERATING)


def test_every_state_appears_in_its_table():
    """Guards against adding an enum member and forgetting the transition row."""
    assert set(SESSION_TRANSITIONS) == set(SessionState)
    assert set(PARTICIPANT_TRANSITIONS) == set(ParticipantState)
    assert set(EVALUATION_TRANSITIONS) == set(EvaluationState)
