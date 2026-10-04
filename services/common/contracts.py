"""Pydantic mirrors of the JSON Schemas in `claude/docs/contracts/`.

The JSON Schema files are the canonical spec; these models are the runtime
enforcement of them, exactly as `contracts/README.md` prescribes. Two rules
follow from that and are enforced here:

1. Nothing improvises a shape. Every cross-component payload is one of these.
2. Every LLM/agent output is validated before it flows downstream — see
   `validate_agent_output`, which checks the Pydantic model *and* the raw JSON
   Schema, so drift between the two is caught rather than papered over.
"""

import functools
import json
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, model_validator

from services.common.config import REPO_ROOT

CONTRACTS_DIR = REPO_ROOT / "claude" / "docs" / "contracts"

# Dimensions an evaluator agent may report on (evaluation_output.schema.json).
DIMENSIONS = (
    "content",
    "communication",
    "leadership",
    "collaboration",
    "critical_thinking",
    "behavior",
)


class ContractError(ValueError):
    """Raised when data does not satisfy its contract. Callers reject/retry."""


@functools.cache
def load_schema(name: str) -> dict[str, Any]:
    """Read a JSON Schema from disk. Cached — these files never change at runtime."""
    path = CONTRACTS_DIR / f"{name}.schema.json"
    if not path.exists():
        raise ContractError(f"no contract named {name} in {CONTRACTS_DIR}")
    return json.loads(path.read_text(encoding="utf-8"))


def validate_against_schema(name: str, payload: dict[str, Any]) -> None:
    """Validate a payload against the canonical JSON Schema.

    `jsonschema` is an explicit dependency rather than an optional nicety: the
    schema files are the source of truth, so the check that they are actually
    honoured cannot be best-effort.
    """
    import jsonschema

    try:
        jsonschema.validate(payload, load_schema(name))
    except jsonschema.ValidationError as exc:
        raise ContractError(f"{name}: {exc.message}") from exc


class TranscriptSegment(BaseModel):
    """`transcript_segment.schema.json` (design §11, §13).

    The source of truth for everything downstream. Persisted the moment it
    exists — never buffered to session end (claude.md invariant 2).
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    start: float = Field(ge=0)
    end: float = Field(ge=0)
    text: str
    confidence: float = Field(ge=0, le=1)

    @model_validator(mode="after")
    def check_interval(self) -> "TranscriptSegment":
        if self.end < self.start:
            raise ValueError("segment end must be >= start")
        return self

    @property
    def duration(self) -> float:
        return self.end - self.start


class CandidateMetrics(BaseModel):
    """`candidate_metrics.schema.json` (design §17, §18).

    Every field here is computed in code from speech events. An LLM may read
    these; it may never produce them (claude.md invariant 4).
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    candidate_id: str = Field(min_length=1)
    speaking_seconds: float = Field(ge=0)
    participation_ratio: float = Field(ge=0, le=1)
    turns: int = Field(ge=0)
    interruptions: int = Field(ge=0)
    responses: int = Field(ge=0)
    questions: int = Field(ge=0)
    arguments_introduced: int = Field(default=0, ge=0)
    arguments_supported: int = Field(default=0, ge=0)
    arguments_challenged: int = Field(default=0, ge=0)
    response_latency_seconds: float = Field(default=0.0, ge=0)


class RollingState(BaseModel):
    """`rolling_state.schema.json` (design §14.4, §16, §47).

    Compressed understanding, not evidence, and never scores — the Observer
    Agent is forbidden from emitting per-candidate scores mid-session
    (claude.md invariants 3 and 7).
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    main_arguments: list[str] = Field(default_factory=list)
    candidate_positions: dict[str, str] = Field(default_factory=dict)
    recent_interactions: list[str] = Field(default_factory=list)
    discussion_direction: str = ""
    unresolved_questions: list[str] = Field(default_factory=list)
    observations: list[str] = Field(default_factory=list)

    @classmethod
    def empty(cls, session_id: str) -> "RollingState":
        """The state the Observer starts from before the first chunk."""
        return cls(session_id=session_id, discussion_direction="not yet established")


class EvidenceItem(BaseModel):
    model_config = ConfigDict(extra="forbid")

    timestamp: str
    summary: str


class EvaluationOutput(BaseModel):
    """`evaluation_output.schema.json` (design §50).

    One agent, one candidate, one dimension. Note there is no overall score
    field: agents never emit the final number, the aggregator computes it
    (claude.md invariant 5).
    """

    model_config = ConfigDict(extra="forbid")

    candidate_id: str = Field(min_length=1)
    dimension: str
    score: float = Field(ge=0, le=10)
    confidence: float = Field(ge=0, le=1)
    strengths: list[str] = Field(default_factory=list)
    weaknesses: list[str] = Field(default_factory=list)
    evidence: list[EvidenceItem] = Field(default_factory=list)

    @model_validator(mode="after")
    def check_dimension(self) -> "EvaluationOutput":
        if self.dimension not in DIMENSIONS:
            raise ValueError(f"unknown dimension {self.dimension!r}")
        return self


class EvaluationVersions(BaseModel):
    """Reproducibility triple (design §40)."""

    model_config = ConfigDict(extra="forbid")

    rubric_version: str
    prompt_version: str
    agent_version: str


class EvaluationArtifacts(BaseModel):
    model_config = ConfigDict(extra="forbid")

    transcript_ref: str | None = None
    final_rolling_state_ref: str | None = None
    metrics_ref: str | None = None


class EvaluationJob(BaseModel):
    """`evaluation_job.schema.json` (design §20, §39, §40).

    `session_id + evaluation_version` is the idempotency key: reprocessing must
    never produce a second, inconsistent report.
    """

    model_config = ConfigDict(extra="forbid")

    session_id: str = Field(min_length=1)
    evaluation_version: int = Field(ge=1)
    topic: str
    gd_type: str
    rubric: dict[str, Any]
    candidate_ids: list[str] = Field(default_factory=list)
    artifacts: EvaluationArtifacts = Field(default_factory=EvaluationArtifacts)
    versions: EvaluationVersions

    @property
    def idempotency_key(self) -> str:
        return f"{self.session_id}:{self.evaluation_version}"


_SCHEMA_FOR: dict[type[BaseModel], str] = {
    TranscriptSegment: "transcript_segment",
    CandidateMetrics: "candidate_metrics",
    RollingState: "rolling_state",
    EvaluationJob: "evaluation_job",
    EvaluationOutput: "evaluation_output",
}


def schema_name_for(model: type[BaseModel]) -> str:
    name = _SCHEMA_FOR.get(model)
    if name is None:
        raise ContractError(f"{model.__name__} has no contract file")
    return name


def validate_contract(instance: BaseModel) -> BaseModel:
    """Belt-and-braces check of a built instance against its JSON Schema.

    Catches the case where the Pydantic model and the canonical schema have
    drifted apart, which is exactly the failure the contracts README warns
    about.
    """
    payload = json.loads(instance.model_dump_json(exclude_none=True))
    validate_against_schema(schema_name_for(type(instance)), payload)
    return instance


def parse_agent_output(raw: Any) -> EvaluationOutput:
    """The validation gate every LLM evaluator result passes through.

    Accepts the model's raw JSON, rejects anything malformed with a
    `ContractError` the caller retries on. Nothing unvalidated reaches the
    aggregator (contracts README, "Validation gate").
    """
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContractError(f"agent output is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ContractError(f"agent output must be a JSON object, got {type(raw).__name__}")

    try:
        output = EvaluationOutput.model_validate(raw)
    except Exception as exc:
        raise ContractError(f"agent output failed the evaluation_output contract: {exc}") from exc

    validate_contract(output)
    return output


def parse_rolling_state(raw: Any, session_id: str) -> RollingState:
    """Validation gate for Observer Agent output (claude.md conventions)."""
    if isinstance(raw, str):
        try:
            raw = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ContractError(f"rolling state is not valid JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise ContractError("rolling state must be a JSON object")

    # The model is told the session id; it is not trusted to echo it back.
    raw = dict(raw)
    raw["session_id"] = session_id
    try:
        state = RollingState.model_validate(raw)
    except Exception as exc:
        raise ContractError(f"rolling state failed its contract: {exc}") from exc

    validate_contract(state)
    return state
