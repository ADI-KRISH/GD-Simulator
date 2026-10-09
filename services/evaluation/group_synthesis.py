"""Group report agent: writes the narrative for the whole discussion.

The per-candidate synthesis agent explains one person. Nothing explained the group:
what the discussion was like, how the floor was shared, where it turned. This agent
reads the finished results (scores, measured behaviour, the Observer's rolling state
and each candidate's synthesis) and writes that narrative.

Like the candidate synthesis, it is the last stage and cannot change a number: it is
handed the computed results as text and returns prose only. A failure costs the
narrative and nothing else, so it never raises.
"""

import logging
from typing import Any

from services.common.contracts import RollingState
from services.evaluation.aggregator import AggregatedScore
from services.llm import get_llm_provider
from services.metrics.engine import SessionMetrics

logger = logging.getLogger(__name__)

GROUP_PROMPT_VERSION = "1.0"

SYSTEM_PROMPT = """\
You are the Group Report agent in a group discussion evaluation system.

Specialist agents have already scored every candidate and a deterministic \
aggregator has produced the final scores. Measured behaviour (speaking time, \
turns, interruptions) was computed in code. Your job is to write the report on \
the discussion as a whole, the way an experienced GD panelist would debrief it.

Rules:
1. Do not invent or recompute any number. Only quote figures that appear in the \
input, exactly as given.
2. Refer to candidates by their label and name as given.
3. Be specific: name who drove the discussion, who was cut off, who stayed \
silent, and what changed the direction of the conversation. Do not give generic \
advice that could apply to any discussion.
4. If the input shows interruptions, say who interrupted whom only if the input \
supports it. If it doesn't say, do not guess.
5. Return JSON only."""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["overview", "group_dynamics", "turning_point", "recommendations"],
    "properties": {
        "overview": {
            "type": "string",
            "description": "Three or four sentences on how the discussion went overall.",
        },
        "group_dynamics": {
            "type": "string",
            "description": "How the floor was shared: who led, who was dominated, interruptions.",
        },
        "turning_point": {
            "type": "string",
            "description": "The moment or exchange that most shaped the discussion.",
        },
        "recommendations": {
            "type": "array",
            "items": {"type": "string"},
            "description": "Two or three concrete things this group should do differently.",
        },
    },
}


def build_prompt(
    topic: str,
    scores: dict[str, AggregatedScore],
    names: dict[str, str],
    metrics: SessionMetrics,
    state: RollingState,
    syntheses: dict[str, Any],
) -> str:
    rows = []
    for label, score in sorted(scores.items()):
        m = metrics.per_candidate.get(label)
        dims = ", ".join(f"{d} {v:.1f}" for d, v in score.dimensions.items())
        behaviour = (
            f"speaking {m.speaking_seconds:.0f}s ({m.participation_ratio * 100:.0f}% of floor), "
            f"{m.turns} turns, {m.interruptions} interruptions made, {m.questions} questions"
            if m
            else "no measured data"
        )
        summary = getattr(syntheses.get(label), "summary", "") or "(no narrative)"
        rows.append(
            f"  {label} ({names.get(label, label)}): overall {score.overall:.1f}/100 "
            f"[{dims}]\n    behaviour: {behaviour}\n    candidate summary: {summary}"
        )

    edges = (
        "; ".join(f"{e.source}->{e.target} x{e.count}" for e in metrics.interactions[:12])
        or "none recorded"
    )
    return f"""\
TOPIC
{topic}

CANDIDATES
{chr(10).join(rows)}

WHO RESPONDED TO WHOM (measured): {edges}

OBSERVER'S RUNNING NOTES (observations, not scores)
  main arguments: {"; ".join(state.main_arguments) or "(none)"}
  positions: {"; ".join(f"{k}: {v}" for k, v in state.candidate_positions.items()) or "(none)"}
  direction: {state.discussion_direction or "(none)"}
  unresolved: {"; ".join(state.unresolved_questions) or "(none)"}
  observations: {"; ".join(state.observations) or "(none)"}

Write the group report."""


async def synthesise_group(
    topic: str,
    scores: dict[str, AggregatedScore],
    names: dict[str, str],
    metrics: SessionMetrics,
    state: RollingState,
    syntheses: dict[str, Any],
) -> dict[str, Any]:
    """Run the group report agent. Returns {} on any failure; never raises."""
    if not scores:
        return {}
    try:
        result = await get_llm_provider().complete_json(
            system=SYSTEM_PROMPT,
            prompt=build_prompt(topic, scores, names, metrics, state, syntheses),
            schema=RESPONSE_SCHEMA,
            max_tokens=1200,
        )
    except Exception as exc:
        logger.warning("group report agent failed", extra={"error": str(exc)})
        return {}
    data = result.data
    return {
        "overview": str(data.get("overview", "")),
        "group_dynamics": str(data.get("group_dynamics", "")),
        "turning_point": str(data.get("turning_point", "")),
        "recommendations": [str(r) for r in data.get("recommendations", [])],
        "model": result.model,
        "prompt_version": GROUP_PROMPT_VERSION,
    }
