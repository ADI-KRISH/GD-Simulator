"""Specialized evaluator agents (design §22, §24–§29, §50).

An agent is a name, a dimension, and a rubric — not a class hierarchy. They all
do the same thing (read the shared context plus one candidate's evidence packet,
emit one contract-shaped verdict), so what distinguishes them is the prompt, and
a dataclass expresses that more honestly than six near-identical subclasses.

Two structural guarantees:

* The response schema pins `dimension` and `candidate_id` to single-value enums,
  so an agent physically cannot score the wrong candidate or wander outside its
  dimension.
* Nothing returns an overall score — the schema has no field for one
  (claude.md invariant 5).
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Any

from services.common.contracts import (
    ContractError,
    EvaluationOutput,
    load_schema,
    parse_agent_output,
)
from services.evaluation.evidence import EvidencePacket, SharedContext
from services.llm import LLMError, get_llm_provider
from services.llm.base import LLMResult, ToolDefinition

logger = logging.getLogger(__name__)

BASE_SYSTEM_PROMPT = """\
You are a specialist evaluator in a group discussion (GD) assessment system.

You assess ONE candidate on ONE dimension, using the evidence you are given.

Non-negotiable rules:
1. Every score must be supported by evidence from the transcript. Cite real \
timestamps and describe what actually happened. A score without evidence is \
invalid output.
2. The measured behaviour section is computed in code from speech events. It is \
ground truth. Never contradict it, never re-estimate it, never guess numbers \
that are already given to you.
3. Judge only your assigned dimension. Another agent covers the others.
4. Do not produce an overall or final score. You produce one dimension score; a \
deterministic aggregator combines them.
5. Set `confidence` honestly. If the candidate barely spoke, or the evidence is \
thin, say so with a low confidence rather than inventing a confident verdict.
6. Score on 0-10 against the dimension rubric below. Be calibrated: 5 is an \
average performance for this dimension, not a failure.
7. When you are shown several candidates at once, score each one against the \
RUBRIC, not against each other. 5 means "average for this dimension in \
general", NOT "average for this group". A strong group must not drag everyone \
down and a weak group must not lift everyone up. Rank order may inform your \
reasoning about comparative dimensions like leadership; it must not set the \
absolute numbers."""


@dataclass(frozen=True, slots=True)
class EvaluatorAgent:
    """One specialist. `dimension` must be a value in the evaluation_output enum."""

    name: str
    dimension: str
    focus: str
    rubric: str
    # Design §29: the Behavior agent is the one that leans on the deterministic
    # metrics as primary evidence rather than as background.
    metrics_led: bool = False

    def system_prompt(self) -> str:
        return (
            f"{BASE_SYSTEM_PROMPT}\n\n"
            f"YOUR DIMENSION: {self.dimension}\n\n"
            f"{self.focus}\n\n"
            f"RUBRIC\n{self.rubric}"
        )

    def user_prompt(self, context: SharedContext, packet: EvidencePacket) -> str:
        emphasis = (
            "\nWeight the measured behaviour heavily — it is the primary evidence "
            "for this dimension.\n"
            if self.metrics_led
            else ""
        )
        return f"""\
=== SHARED DISCUSSION CONTEXT ===
{context.render()}

=== CANDIDATE EVIDENCE ===
{packet.render()}
{emphasis}
Assess candidate {packet.candidate_label} on {self.dimension}. Return your \
assessment as JSON."""

    def response_schema(self, candidate_label: str) -> dict[str, Any]:
        """The evaluation_output contract, pinned to this agent and candidate."""
        schema = json.loads(json.dumps(load_schema("evaluation_output")))
        schema.pop("$schema", None)
        schema.pop("$id", None)
        schema.pop("examples", None)
        schema["properties"]["dimension"] = {"type": "string", "enum": [self.dimension]}
        schema["properties"]["candidate_id"] = {"type": "string", "enum": [candidate_label]}
        return schema

    # ----------------------------------------------------------------------
    # Batched path: one call per specialist, covering every candidate.
    #
    # Design §49 asks for exactly this — "avoid sending enormous duplicate
    # prompts to every agent" — and it is also better evaluation, not just
    # cheaper: leadership, dominance and participation balance are inherently
    # comparative, and a judge that can see the whole group can say "A drove
    # direction, C asked the unsticking question, F never initiated". Judging
    # each candidate in a separate call makes that impossible.
    # ----------------------------------------------------------------------

    def batch_user_prompt(
        self, context: SharedContext, packets: dict[str, EvidencePacket]
    ) -> str:
        emphasis = (
            "\nWeight the measured behaviour heavily — it is the primary evidence "
            "for this dimension.\n"
            if self.metrics_led
            else ""
        )
        labels = sorted(packets)
        blocks = "\n\n".join(packets[label].render() for label in labels)
        return f"""\
=== SHARED DISCUSSION CONTEXT ===
{context.render()}

=== CANDIDATE EVIDENCE ({len(labels)} candidates) ===
{blocks}
{emphasis}
Assess EVERY candidate ({", ".join(labels)}) on {self.dimension}. Return exactly \
one assessment per candidate, in that order. Do not omit anyone and do not \
invent a candidate who is not listed above."""

    def batch_response_schema(self, candidate_labels: list[str]) -> dict[str, Any]:
        """One assessment per candidate, each still the evaluation_output contract.

        `candidate_id` is an enum of the real labels, so the model cannot invent
        a participant. It cannot pin to a single value the way the per-candidate
        path does, which is why `parse_batch_output` additionally checks for
        duplicates and omissions.
        """
        item = json.loads(json.dumps(load_schema("evaluation_output")))
        item.pop("$schema", None)
        item.pop("$id", None)
        item.pop("examples", None)
        item["properties"]["dimension"] = {"type": "string", "enum": [self.dimension]}
        item["properties"]["candidate_id"] = {
            "type": "string",
            "enum": sorted(candidate_labels),
        }
        return {
            "type": "object",
            "additionalProperties": False,
            "required": ["assessments"],
            "properties": {
                "assessments": {
                    "type": "array",
                    "minItems": len(candidate_labels),
                    "maxItems": len(candidate_labels),
                    "items": item,
                }
            },
        }


TOOL_GUIDANCE = (
    "\n\nTOOLS: you can look things up before you score. Use get_turns and "
    "get_candidate_metrics for interruptions and replies, find_quotes and "
    "get_transcript_range to verify a moment and quote it exactly. Every piece of "
    "evidence you cite must be a time at which that candidate really spoke. "
    "Interruptions are penalised by the system itself - do not lower your "
    "dimension score for them a second time."
)


async def _complete(
    provider,
    agent: "EvaluatorAgent",
    prompt: str,
    schema: dict[str, Any],
    tools: list[ToolDefinition] | None,
) -> LLMResult:
    """One model call: tool-using when evidence tools are supplied, else plain JSON."""
    if tools:
        return await provider.complete_with_tools(
            system=agent.system_prompt() + TOOL_GUIDANCE,
            prompt=prompt,
            tools=tools,
            schema=schema,
        )
    return await provider.complete_json(system=agent.system_prompt(), prompt=prompt, schema=schema)


def _calls(result: LLMResult) -> list[dict[str, Any]]:
    """Tool-call provenance, trimmed so the audit trail stays small."""
    return [
        {"name": c.name, "arguments": c.arguments, "result": str(c.result)[:400]}
        for c in result.tool_calls
    ]


@dataclass(slots=True)
class AgentRun:
    """Outcome of one agent on one candidate. Failure is a value, not an
    exception: design §21 has the Supervisor resolve missing results, which it
    can only do if it is told what is missing."""

    agent: str
    dimension: str
    candidate_label: str
    output: EvaluationOutput | None
    error: str | None = None
    # What the agent asked its tools and what the critic said (audit trail).
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    critic: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return self.output is not None


async def run_agent(
    agent: EvaluatorAgent,
    context: SharedContext,
    packet: EvidencePacket,
    tools: list[ToolDefinition] | None = None,
    feedback: str = "",
) -> AgentRun:
    """Invoke one agent and validate its output against the contract.

    Nothing unvalidated escapes this function — the contracts README calls for a
    gate wrapping every agent call, and this is it.
    """
    provider = get_llm_provider()
    try:
        prompt = agent.user_prompt(context, packet)
        if feedback:
            prompt += f"\n\nREVIEWER FEEDBACK on your previous answer - fix this:\n{feedback}"
        result = await _complete(
            provider, agent, prompt, agent.response_schema(packet.candidate_label), tools
        )
        output = parse_agent_output(result.data)
    except (LLMError, ContractError) as exc:
        logger.warning(
            "evaluator agent failed",
            extra={
                "agent": agent.name,
                "dimension": agent.dimension,
                "candidate": packet.candidate_label,
                "error": str(exc),
            },
        )
        return AgentRun(
            agent=agent.name,
            dimension=agent.dimension,
            candidate_label=packet.candidate_label,
            output=None,
            error=str(exc),
        )

    # The pinned enums make this near-impossible, but a mismatch here would
    # silently score the wrong person, so it is checked rather than assumed.
    if output.candidate_id != packet.candidate_label:
        return AgentRun(
            agent=agent.name,
            dimension=agent.dimension,
            candidate_label=packet.candidate_label,
            output=None,
            error=(
                f"agent returned candidate {output.candidate_id!r}, "
                f"expected {packet.candidate_label!r}"
            ),
        )

    return AgentRun(
        agent=agent.name,
        dimension=agent.dimension,
        candidate_label=packet.candidate_label,
        output=output,
        tool_calls=_calls(result),
    )


def parse_batch_output(
    agent: EvaluatorAgent, raw: Any, expected: list[str]
) -> list[EvaluationOutput]:
    """Validate a batched response: one assessment per candidate, no more, no less.

    The schema stops invented candidates; this stops the two failures a schema
    cannot express — the same candidate assessed twice, and a candidate silently
    skipped. Both would corrupt scoring quietly, so they are rejected loudly.
    """
    if not isinstance(raw, dict) or "assessments" not in raw:
        raise ContractError("batched output has no 'assessments' array")

    outputs = [parse_agent_output(item) for item in raw["assessments"]]
    seen = [o.candidate_id for o in outputs]

    duplicates = {label for label in seen if seen.count(label) > 1}
    if duplicates:
        raise ContractError(f"candidate assessed more than once: {', '.join(sorted(duplicates))}")

    missing = set(expected) - set(seen)
    if missing:
        raise ContractError(f"no assessment for: {', '.join(sorted(missing))}")

    wrong_dimension = {o.dimension for o in outputs} - {agent.dimension}
    if wrong_dimension:
        raise ContractError(
            f"{agent.name} returned dimensions it does not own: "
            f"{', '.join(sorted(wrong_dimension))}"
        )

    return outputs


async def run_agent_batch(
    agent: EvaluatorAgent,
    context: SharedContext,
    packets: dict[str, EvidencePacket],
    tools: list[ToolDefinition] | None = None,
) -> list[AgentRun]:
    """Invoke one specialist once for every candidate (design §49).

    Returns one `AgentRun` per candidate either way: on failure they are all
    failed runs carrying the same error, so the Supervisor's collect-and-resolve
    step treats a batched failure exactly like a per-candidate one and the
    aggregator's missing-dimension handling applies unchanged.
    """
    labels = sorted(packets)
    provider = get_llm_provider()

    try:
        result = await _complete(
            provider,
            agent,
            agent.batch_user_prompt(context, packets),
            agent.batch_response_schema(labels),
            tools,
        )
        outputs = parse_batch_output(agent, result.data, labels)
    except (LLMError, ContractError) as exc:
        logger.warning(
            "batched evaluator agent failed",
            extra={
                "agent": agent.name,
                "dimension": agent.dimension,
                "candidates": len(labels),
                "error": str(exc),
            },
        )
        return [
            AgentRun(
                agent=agent.name,
                dimension=agent.dimension,
                candidate_label=label,
                output=None,
                error=str(exc),
            )
            for label in labels
        ]

    by_label = {output.candidate_id: output for output in outputs}
    return [
        AgentRun(
            agent=agent.name,
            dimension=agent.dimension,
            candidate_label=label,
            output=by_label[label],
            tool_calls=_calls(result),
        )
        for label in labels
    ]
