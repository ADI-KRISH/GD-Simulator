"""Contract validation (contracts/README.md, claude.md conventions).

The JSON Schemas are the source of truth, the Pydantic models are the runtime
enforcement, and these tests hold the two together — plus they exercise the
validation gate that every agent output must pass before it reaches the
aggregator.

Purpose:    check that each data contract (transcript segment, metrics, rolling
            state, evaluation output, evaluation job) accepts good data and
            rejects bad data, in both the Pydantic model and the JSON Schema.
Why needed: LLMs return free-form JSON; these tests guarantee a malformed answer
            (wrong dimension, out-of-range score, smuggled "overall" score) is
            stopped before it can corrupt a score.
Contains:
    test_every_contract_file_loads, test_unknown_contract_name_is_an_error -
        the schema files exist and unknown names fail loudly.
    test_transcript_segment_*, test_a_segment_cannot_end_before_it_starts,
    test_contracts_reject_unknown_fields, test_confidence_is_bounded -
        transcript segment rules.
    test_candidate_metrics_*, test_participation_ratio_cannot_exceed_one - metrics rules.
    test_empty_rolling_state_is_valid, test_rolling_state_* - Observer state rules.
    test_the_agent_validation_gate_*, test_the_gate_* - the gate accepts good
        agent output and rejects malformed output.
    test_agents_cannot_smuggle_an_overall_score_through - claude.md invariant 5.
    test_evaluation_job_*, test_evaluation_output_has_no_field_for_a_final_score -
        job idempotency key and the structural "no final score" guarantee.
Used by:    pytest.
Depends on: common/contracts (and, through it, common/config and the schema
            files in claude/docs/contracts/).
"""

import pytest

from services.common.contracts import (
    CandidateMetrics,
    ContractError,
    EvaluationJob,
    EvaluationOutput,
    EvaluationVersions,
    RollingState,
    TranscriptSegment,
    load_schema,
    parse_agent_output,
    parse_rolling_state,
    validate_contract,
)


def test_every_contract_file_loads():
    """All five JSON Schema contract files load."""
    # Each named schema must exist on disk and carry a title.
    for name in (
        "transcript_segment",
        "candidate_metrics",
        "rolling_state",
        "evaluation_job",
        "evaluation_output",
    ):
        assert load_schema(name)["title"]


def test_unknown_contract_name_is_an_error():
    """Asking for an unknown contract raises ContractError."""
    with pytest.raises(ContractError):
        load_schema("not_a_contract")


def test_transcript_segment_round_trips_its_schema():
    """A TranscriptSegment validates against its JSON Schema."""
    # Build a valid segment, then check it against the canonical schema too.
    segment = TranscriptSegment(
        session_id="GD123",
        candidate_id="A",
        start=42.1,
        end=48.8,
        text="I think AI will create new categories of employment.",
        confidence=0.94,
    )
    validate_contract(segment)
    assert segment.duration == pytest.approx(6.7)


def test_a_segment_cannot_end_before_it_starts():
    """A segment ending before it starts is rejected."""
    # end=5 < start=10 would give a negative speaking duration.
    with pytest.raises(ValueError):
        TranscriptSegment(
            session_id="GD123", candidate_id="A", start=10.0, end=5.0, text="x", confidence=0.9
        )


def test_contracts_reject_unknown_fields():
    """`additionalProperties: false` in the schema, `extra="forbid"` in the model."""
    # An invented field (speaker_name) must be refused, not silently dropped.
    with pytest.raises(ValueError):
        TranscriptSegment(
            session_id="GD123",
            candidate_id="A",
            start=0.0,
            end=1.0,
            text="x",
            confidence=0.9,
            speaker_name="Alice",
        )


def test_confidence_is_bounded():
    """Confidence above 1 is rejected."""
    with pytest.raises(ValueError):
        TranscriptSegment(
            session_id="G", candidate_id="A", start=0, end=1, text="x", confidence=1.5
        )


def test_candidate_metrics_validate_against_the_schema():
    """CandidateMetrics validates against its schema."""
    metrics = CandidateMetrics(
        session_id="GD123",
        candidate_id="A",
        speaking_seconds=245,
        participation_ratio=0.29,
        turns=17,
        interruptions=2,
        responses=8,
        questions=3,
    )
    validate_contract(metrics)


def test_participation_ratio_cannot_exceed_one():
    """A participation ratio above 1 is rejected."""
    with pytest.raises(ValueError):
        CandidateMetrics(
            session_id="G",
            candidate_id="A",
            speaking_seconds=1,
            participation_ratio=1.4,
            turns=1,
            interruptions=0,
            responses=0,
            questions=0,
        )


def test_empty_rolling_state_is_valid():
    """The empty starting rolling state is a valid contract."""
    state = RollingState.empty("GD123")
    validate_contract(state)
    assert state.candidate_positions == {}


def test_the_agent_validation_gate_accepts_a_good_output():
    """A well-formed agent output passes the validation gate."""
    # A realistic collaboration result, exactly as an LLM would return it.
    raw = {
        "candidate_id": "A",
        "dimension": "collaboration",
        "score": 8.7,
        "confidence": 0.89,
        "strengths": ["Built on B's argument"],
        "weaknesses": ["Could have invited another participant"],
        "evidence": [{"timestamp": "05:14", "summary": "A acknowledged B's concern."}],
    }
    output = parse_agent_output(raw)
    assert output.dimension == "collaboration"
    assert output.score == 8.7


def test_the_gate_parses_a_json_string():
    """The gate accepts raw JSON text."""
    output = parse_agent_output(
        '{"candidate_id":"A","dimension":"content","score":7,"confidence":0.8,'
        '"strengths":[],"weaknesses":[],"evidence":[]}'
    )
    assert output.score == 7


@pytest.mark.parametrize(
    "raw",
    [
        "not json at all",
        ["a", "list"],
        {"candidate_id": "A"},  # missing required fields
        {
            "candidate_id": "A",
            "dimension": "vibes",  # not in the enum
            "score": 5,
            "confidence": 0.5,
            "strengths": [],
            "weaknesses": [],
            "evidence": [],
        },
        {
            "candidate_id": "A",
            "dimension": "content",
            "score": 42,  # out of the 0-10 range
            "confidence": 0.5,
            "strengths": [],
            "weaknesses": [],
            "evidence": [],
        },
    ],
)
def test_the_gate_rejects_malformed_agent_output(raw):
    """Nothing unvalidated reaches the aggregator."""
    with pytest.raises(ContractError):
        parse_agent_output(raw)


def test_agents_cannot_smuggle_an_overall_score_through():
    """claude.md invariant 5: only the aggregator produces the final number."""
    # An extra "overall_score" field is exactly the leak the contract must block.
    with pytest.raises(ContractError):
        parse_agent_output(
            {
                "candidate_id": "A",
                "dimension": "content",
                "score": 8,
                "overall_score": 84,
                "confidence": 0.9,
                "strengths": [],
                "weaknesses": [],
                "evidence": [],
            }
        )


def test_rolling_state_session_id_is_forced_not_trusted():
    """A model echoing the wrong session id must not write state elsewhere."""
    # The parser must overwrite the model's session id with the one we know is right.
    state = parse_rolling_state(
        {
            "session_id": "SOMEONE-ELSES-SESSION",
            "main_arguments": ["AI increases productivity"],
            "candidate_positions": {"A": "pro-automation"},
            "recent_interactions": ["A responded to B"],
            "discussion_direction": "employment impact",
            "unresolved_questions": [],
        },
        "GD123",
    )
    assert state.session_id == "GD123"


def test_rolling_state_cannot_carry_scores():
    """claude.md invariant 7: the Observer produces observations, not scores."""
    # A "scores" field in the rolling state must be rejected as an unknown field.
    with pytest.raises(ContractError):
        parse_rolling_state(
            {
                "main_arguments": [],
                "candidate_positions": {},
                "recent_interactions": [],
                "discussion_direction": "x",
                "unresolved_questions": [],
                "scores": {"A": 8.5},
            },
            "GD123",
        )


def test_evaluation_job_idempotency_key():
    """Design §39: session_id + evaluation_version."""
    # A complete job (with its version triple) must validate and expose its key.
    job = EvaluationJob(
        session_id="GD123",
        evaluation_version=2,
        topic="Should AI-generated content be regulated?",
        gd_type="technical",
        rubric={"weights": {"content": 0.3}},
        candidate_ids=["A", "B"],
        versions=EvaluationVersions(
            rubric_version="2.1", prompt_version="4.0", agent_version="1.8"
        ),
    )
    validate_contract(job)
    assert job.idempotency_key == "GD123:2"


def test_evaluation_job_requires_its_version_triple():
    """Design §40: no evaluation without a reproducible configuration."""
    # Omitting `versions` must fail: a score has to be reproducible later.
    with pytest.raises(ValueError):
        EvaluationJob(
            session_id="GD123",
            evaluation_version=1,
            topic="x",
            gd_type="general",
            rubric={},
        )


def test_evaluation_output_has_no_field_for_a_final_score():
    """Structural guarantee, not a convention."""
    assert "overall" not in EvaluationOutput.model_fields
    assert "final_score" not in EvaluationOutput.model_fields
