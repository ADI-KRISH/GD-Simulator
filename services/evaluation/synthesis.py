"""The Final Synthesis agent (design §20 "Final Evaluator", §29, §53).

The last stage of the pipeline, and the one place a model is allowed a holistic
view of a candidate. It reads all five dimension verdicts plus the measured
behaviour and writes the narrative: what the numbers mean together, what to work
on first, and anything the fixed formula could not see.

**It does not compute the score.** claude.md invariant 5 keeps the final number
in code, and this module keeps it there in a specific way: the agent is given
*tools* rather than arithmetic, and the tools accept no numbers.

    compute_final_score(candidate_label)  -> reads the stored dimension scores
                                             and the session rubric, returns the
                                             weighted result
    score_if(candidate_label, dimension, hypothetical_score) -> a what-if

The model chooses *which question to ask*; the answer always comes from
`aggregator.aggregate_candidate` operating on data the model cannot influence.
So a hallucinated number is impossible by construction, not by instruction — and
because the what-if tool exists, the feedback can be specific and arithmetically
true: "collaboration at 8.0 instead of 5.2 would have put you at 79".

Every tool call is recorded, so a score's narrative is auditable: you can show
exactly what the model asked and what it was told.
"""

import logging
from dataclasses import asdict, dataclass, field
from typing import Any

from services.common.contracts import CandidateMetrics, EvaluationOutput
from services.evaluation.aggregator import AggregatedScore, aggregate_candidate
from services.llm import get_llm_provider
from services.llm.base import ToolDefinition

logger = logging.getLogger(__name__)

SYNTHESIS_PROMPT_VERSION = "1.0"

SYSTEM_PROMPT = """\
You are the Final Synthesis agent in a group discussion evaluation system.

Five specialist evaluators have already scored this candidate on their own \
dimensions, each citing transcript evidence. A deterministic aggregator has \
already combined those into a final score using the session's fixed weights.

Your job is the part none of them could do: read the whole picture and write \
the candidate's feedback.

Rules:
1. You do NOT decide the score. It is already computed. Call \
`compute_final_score` to find out what it is — do not estimate it, do not \
recalculate it in your head, and never state a number you were not given by a \
tool.
2. Use `score_if` when you want to say what a change would be worth. Only quote \
numbers a tool returned.
3. Say what the dimensions mean *together*. "Strong content, weak \
collaboration" is a pattern with a cause — name it. That is the value you add \
over the individual reports.
4. Give ONE priority improvement, the one that would matter most. Not a list of \
five.
5. If the numbers look wrong to you — the score seems higher or lower than the \
evidence justifies — say so in `caveat`. Flagging it is useful; silently \
disagreeing is not. You cannot change the score and should not try.
6. Address the candidate as "you". Be direct and specific. No praise sandwiches, \
no filler.
"""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["summary", "pattern", "priority_improvement", "caveat"],
    "properties": {
        "summary": {
            "type": "string",
            "description": "Two or three sentences on the overall performance.",
        },
        "pattern": {
            "type": "string",
            "description": "What the dimension scores mean together, and why.",
        },
        "priority_improvement": {
            "type": "string",
            "description": "The single change that would matter most next time.",
        },
        "caveat": {
            "type": "string",
            "description": (
                "Anything the fixed formula missed, or an empty string. Not a "
                "score change - an observation for a human reviewer."
            ),
        },
    },
}


@dataclass(slots=True)
class Synthesis:
    """The narrative layer of a candidate report."""

    summary: str
    pattern: str
    priority_improvement: str
    caveat: str
    # Provenance: what the model asked the scoring tools and what it was told.
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def unavailable(cls, reason: str) -> "Synthesis":
        """A report without a narrative is still a valid report.

        The scores, dimensions and evidence are all produced upstream, so a
        synthesis failure degrades the report rather than failing the job.
        """
        return cls(
            summary="",
            pattern="",
            priority_improvement="",
            caveat="",
            error=reason,
        )


def build_tools(
    candidate_label: str,
    outputs: list[EvaluationOutput],
    weights: dict[str, float],
) -> list[ToolDefinition]:
    """Scoring tools bound to one candidate's stored results.

    Note what the handlers close over: the agent outputs and the rubric, both
    already persisted. Neither tool accepts a dimension score as an argument
    except in the explicitly hypothetical `score_if`, whose result is never
    stored. The model cannot move the real number.
    """

    def _explain(score: AggregatedScore) -> dict[str, Any]:
        return {
            "candidate": score.candidate_label,
            "overall_out_of_100": score.overall,
            "dimension_scores_out_of_10": score.dimensions,
            "weights_applied": score.weights,
            "confidence": score.confidence,
            "missing_dimensions": score.missing_dimensions,
            "needs_human_review": score.needs_human_review,
        }

    def compute_final_score() -> dict[str, Any]:
        return _explain(aggregate_candidate(candidate_label, outputs, weights))

    def score_if(dimension: str, hypothetical_score: float) -> dict[str, Any]:
        if dimension not in weights:
            return {
                "error": f"unknown dimension {dimension!r}",
                "valid_dimensions": sorted(weights),
            }
        if not 0 <= hypothetical_score <= 10:
            return {"error": "hypothetical_score must be between 0 and 10"}

        # Replace that dimension's outputs with the hypothetical, leave the
        # rest untouched, and re-run the same deterministic aggregator.
        adjusted = [o for o in outputs if o.dimension != dimension]
        template = next((o for o in outputs if o.dimension == dimension), None)
        adjusted.append(
            EvaluationOutput(
                candidate_id=candidate_label,
                dimension=dimension,
                score=hypothetical_score,
                confidence=template.confidence if template else 0.5,
                strengths=[],
                weaknesses=[],
                evidence=[],
            )
        )
        actual = aggregate_candidate(candidate_label, outputs, weights)
        hypothetical = aggregate_candidate(candidate_label, adjusted, weights)
        return {
            "hypothetical": True,
            "dimension": dimension,
            "actual_dimension_score": actual.dimensions.get(dimension),
            "hypothetical_dimension_score": hypothetical_score,
            "actual_overall": actual.overall,
            "hypothetical_overall": hypothetical.overall,
            "difference": round(hypothetical.overall - actual.overall, 2),
        }

    return [
        ToolDefinition(
            name="compute_final_score",
            description=(
                "Return this candidate's actual final score, the per-dimension "
                "scores behind it, and the weights applied. Takes no arguments "
                "- it reads the stored evaluation."
            ),
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            handler=compute_final_score,
        ),
        ToolDefinition(
            name="score_if",
            description=(
                "What the final score WOULD have been if one dimension had "
                "scored differently. Hypothetical only - it changes nothing. "
                "Use it to quantify advice, e.g. what lifting collaboration "
                "would have been worth."
            ),
            input_schema={
                "type": "object",
                "additionalProperties": False,
                "required": ["dimension", "hypothetical_score"],
                "properties": {
                    "dimension": {"type": "string", "enum": sorted(weights)},
                    "hypothetical_score": {"type": "number", "minimum": 0, "maximum": 10},
                },
            },
            handler=score_if,
        ),
    ]


def build_prompt(
    candidate_label: str,
    outputs: list[EvaluationOutput],
    metrics: CandidateMetrics,
    topic: str,
) -> str:
    """Dimension verdicts plus measured behaviour. Deliberately no numbers for
    the overall score — that is what the tool is for."""
    blocks = []
    for output in sorted(outputs, key=lambda o: o.dimension):
        evidence = "\n".join(f"      {e.timestamp} {e.summary}" for e in output.evidence)
        header = (
            f"  {output.dimension.upper()} — {output.score}/10 "
            f"(confidence {output.confidence:.2f})"
        )
        blocks.append(
            f"""{header}
    strengths:  {"; ".join(output.strengths) or "(none noted)"}
    weaknesses: {"; ".join(output.weaknesses) or "(none noted)"}
    evidence:
{evidence or "      (none cited)"}"""
        )

    share = metrics.participation_ratio * 100
    return f"""\
DISCUSSION TOPIC
{topic}

CANDIDATE {candidate_label} — SPECIALIST VERDICTS
{chr(10).join(blocks)}

MEASURED BEHAVIOUR (computed in code from speech events — facts, not estimates)
  speaking time        {metrics.speaking_seconds:.0f}s ({share:.1f}% of the floor)
  turns                {metrics.turns}
  interruptions made   {metrics.interruptions}
  direct responses     {metrics.responses}
  questions asked      {metrics.questions}
  avg response latency {metrics.response_latency_seconds:.1f}s

Call compute_final_score to get the actual score. Use score_if if you want to \
quantify your advice. Then write the candidate's feedback."""


async def synthesise(
    candidate_label: str,
    outputs: list[EvaluationOutput],
    weights: dict[str, float],
    metrics: CandidateMetrics,
    topic: str,
) -> Synthesis:
    """Run the synthesis agent for one candidate. Never raises.

    A failure here costs the narrative and nothing else — the score, dimensions
    and evidence are already computed and stored (claude.md invariant 1).
    """
    if not outputs:
        return Synthesis.unavailable("no dimension scores to synthesise")

    provider = get_llm_provider()
    tools = build_tools(candidate_label, outputs, weights)

    try:
        result = await provider.complete_with_tools(
            system=SYSTEM_PROMPT,
            prompt=build_prompt(candidate_label, outputs, metrics, topic),
            tools=tools,
            schema=RESPONSE_SCHEMA,
        )
    except Exception as exc:
        # Deliberately broad: a synthesis failure must never fail the job, and
        # this is the last stage, so there is nothing downstream to protect.
        logger.warning(
            "synthesis agent failed",
            extra={"candidate": candidate_label, "error": str(exc)},
        )
        return Synthesis.unavailable(str(exc))

    data = result.data
    return Synthesis(
        summary=str(data.get("summary", "")),
        pattern=str(data.get("pattern", "")),
        priority_improvement=str(data.get("priority_improvement", "")),
        caveat=str(data.get("caveat", "")),
        tool_calls=[
            {"tool": call.name, "arguments": call.arguments, "result": call.result}
            for call in result.tool_calls
        ],
        model=result.model,
    )
