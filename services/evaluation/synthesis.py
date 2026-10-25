"""The Final Synthesis agent (design §20 "Final Evaluator", §29, §53).

Purpose:     Writes each candidate's personal feedback narrative (summary, how
             the scores fit together, strengths, focus areas, per-dimension
             explanations) after the score has already been computed.
Why needed:  The fixed formula gives a number but not advice. This agent adds the
             human-readable "what it means and what to do next", while being
             structurally unable to change the score: it only reads it through
             tools that take no numeric input (claude.md invariant 5).
Contains:
  - `SYNTHESIS_PROMPT_VERSION`, `SYNTHESIS_MAX_TOKENS`, `SYSTEM_PROMPT`,
    `RESPONSE_SCHEMA`   the versioned instructions and required JSON shape.
  - `Synthesis`         the narrative container (+ `to_dict`, `unavailable`).
  - `build_tools`       `compute_final_score` and `score_if` (what-if), both
                        backed by the real aggregator.
  - `build_prompt`      verdicts + measured behaviour + the candidate's own lines.
  - `silent_synthesis`  a fixed, encouraging narrative for candidates who said nothing.
  - `_openings_block`, `openings_for`, `own_lines_for`, `_clean_items`  prompt and
        output helpers.
  - `synthesise`        run the agent for one candidate; never raises.
Used by:     evaluation/graph.py, evaluation/service.py, tests/test_synthesis.py.
Depends on:  common/contracts.py, evaluation/aggregator.py (the real score math),
             llm/__init__.py (the model), llm/base.py (`ToolDefinition`).

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

import logging  # warn when synthesis falls back
from dataclasses import asdict, dataclass, field  # Synthesis container and dict export
from typing import Any  # JSON-like tool results

from services.common.contracts import CandidateMetrics, EvaluationOutput
from services.evaluation.aggregator import AggregatedScore, aggregate_candidate  # score math
from services.llm import get_llm_provider  # model that writes the narrative
from services.llm.base import ToolDefinition  # tools the model may call for numbers

logger = logging.getLogger(__name__)

SYNTHESIS_PROMPT_VERSION = "1.1"
# The feedback is long-form; the default token cap would truncate its JSON.
SYNTHESIS_MAX_TOKENS = 4000

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
4. `priority_improvement` is the ONE change that would matter most. \
`focus_areas` is the short ordered plan (two or three items, most important \
first) of what to work on, and its first item must match the priority.
5. If the numbers look wrong to you — the score seems higher or lower than the \
evidence justifies — say so in `caveat`. Flagging it is useful; silently \
disagreeing is not. You cannot change the score and should not try.
6. Address the candidate as "you". Be direct and specific. No praise sandwiches, \
no filler.
7. Make it personal to this candidate. Every strength and every focus area must \
point at something this person actually said or did: quote a short phrase from \
"YOUR OWN LINES" or cite a timestamp and a measured figure. Advice that could be \
pasted into anyone else's report ("communicate more clearly", "listen actively") \
is a failure.
8. For each focus area, say what happened, why it cost them, and give a concrete \
thing to do or say next time (for example the kind of sentence to open with), \
not a trait to have. Where it helps, use `score_if` to say what it was worth.
9. A line ending in "—" was cut off by another speaker; the candidate did not \
trail off. Never fault someone for it. If it happened, it is context (they were \
talked over), and the interruption counts in the measured behaviour show who was \
doing the cutting.
10. `try_saying` is an example of a move, so keep it generic. Do not put \
invented statistics, studies or facts in it; only reuse figures that appear in \
the input.
11. Strengths are things to keep doing. Name what worked and why it helped the \
discussion, not just that it was "good".
12. Write for the candidate, who has not seen the scoring rubric. Use plain \
language and full sentences, and explain any term you use (for example what \
"collaboration" means in a group discussion) instead of assuming they know it. \
Be descriptive: walk through what happened, then what it did to the discussion \
and to how they came across, then what to do. Two to four sentences per field is \
right; a bare phrase is too thin to learn from.
13. For every dimension, explain in `dimension_notes` what that dimension \
measures and what this candidate's score means for them, so the numbers in the \
report are not a mystery.
14. If the candidate spoke little or not at all (see MEASURED BEHAVIOUR), be warm \
and encouraging, never disappointed: not getting in is where nearly everyone \
starts. Open `summary` with that, not with the score or what was missing. Build the \
feedback on the OPENINGS listed: when a natural moment came, and one concrete \
sentence they could have said to take it. Mention the score at most once, plainly, \
and never describe a dimension as being "at the bottom".
"""

RESPONSE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "summary",
        "pattern",
        "priority_improvement",
        "caveat",
        "strengths",
        "focus_areas",
        "dimension_notes",
    ],
    "properties": {
        "dimension_notes": {
            "type": "array",
            "description": "One plain-language explanation per scored dimension.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["dimension", "what_it_measures", "how_you_did"],
                "properties": {
                    "dimension": {"type": "string", "description": "The dimension name."},
                    "what_it_measures": {
                        "type": "string",
                        "description": "One or two sentences on what it looks for.",
                    },
                    "how_you_did": {
                        "type": "string",
                        "description": "Two or three sentences on what their score means, "
                        "with an example from their own lines.",
                    },
                },
            },
        },
        "strengths": {
            "type": "array",
            "maxItems": 3,
            "description": "Things this candidate did well and should keep doing.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["point", "evidence", "why_it_helped"],
                "properties": {
                    "point": {"type": "string", "description": "The strength, in a phrase."},
                    "evidence": {
                        "type": "string",
                        "description": "A short quote of theirs, or a timestamp and figure.",
                    },
                    "why_it_helped": {
                        "type": "string",
                        "description": "Two or three sentences on what it did for the discussion "
                        "and how it made them come across.",
                    },
                },
            },
        },
        "focus_areas": {
            "type": "array",
            "maxItems": 3,
            "description": "What to work on, most important first.",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "area",
                    "what_happened",
                    "why_it_matters",
                    "what_to_do",
                    "try_saying",
                    "practice",
                ],
                "properties": {
                    "area": {"type": "string", "description": "The skill to work on, in a phrase."},
                    "what_happened": {
                        "type": "string",
                        "description": "Two or three sentences on what they did, with a quote, "
                        "timestamp or figure.",
                    },
                    "why_it_matters": {
                        "type": "string",
                        "description": "Two sentences on how this plays out in a real "
                        "group discussion and what it costs the candidate.",
                    },
                    "what_to_do": {
                        "type": "string",
                        "description": "Two or three sentences on the concrete change to make.",
                    },
                    "try_saying": {
                        "type": "string",
                        "description": "An example sentence or move they could use.",
                    },
                    "practice": {
                        "type": "string",
                        "description": "A small exercise to do before the next "
                        "discussion, in one or two sentences.",
                    },
                },
            },
        },
        "summary": {
            "type": "string",
            "description": "Four to six sentences on the overall performance, in plain language.",
        },
        "pattern": {
            "type": "string",
            "description": "Three or four sentences on what the scores mean together, and why.",
        },
        "priority_improvement": {
            "type": "string",
            "description": "Two sentences: the one change that matters most, and why.",
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
    strengths: list[dict[str, str]] = field(default_factory=list)
    focus_areas: list[dict[str, str]] = field(default_factory=list)
    dimension_notes: list[dict[str, str]] = field(default_factory=list)
    # Provenance: what the model asked the scoring tools and what it was told.
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    model: str = ""
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """The synthesis as a plain dict."""
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
        """A score rendered as a plain dict the model can read from a tool result."""
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
        """Tool: the candidate's real final score, computed in code, never by the model."""
        return _explain(aggregate_candidate(candidate_label, outputs, weights))

    def score_if(dimension: str, hypothetical_score: float) -> dict[str, Any]:
        """Tool: what the final score would be if one dimension scored differently."""
        # Reject bad arguments with a readable error the model can correct.
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
        # Score the real and the what-if versions with the same aggregator and compare.
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
    own_lines: list[str] | None = None,
    openings: list[str] | None = None,
) -> str:
    """Dimension verdicts plus measured behaviour. Deliberately no numbers for
    the overall score — that is what the tool is for. `openings`: for a quiet
    candidate, the questions others put to the group (rule 14)."""
    # One text block per dimension: score, confidence, strengths/weaknesses, evidence.
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

YOUR OWN LINES (what this candidate actually said, with timestamps)
{chr(10).join(own_lines) if own_lines else "  (no transcript lines available)"}
{_openings_block(openings)}
Call compute_final_score to get the actual score. Use score_if if you want to \
quantify your advice. Then write the candidate's feedback."""


MAX_OWN_LINES = 12
MAX_LINE_CHARS = 280
#: How many openings a quiet candidate's prompt lists (rule 14).
MAX_OPENINGS = 4
#: A candidate below this share of the floor (or with no turns) is quiet.
QUIET_SHARE = 0.10


def silent_synthesis(openings: list[str]) -> Synthesis:
    """The narrative for a candidate who said nothing (M14 EC-14.1): encouraging,
    and built on the openings they had — never on what they "failed" to show."""
    # Quote the first opening (timestamp + question) when there is one.
    first = openings[0].strip() if openings else ""
    moment = (
        f" The first came at {first.split(' ', 1)[0]}: \"{first.split(' ', 1)[1]}\""
        if " " in first
        else ""
    )
    return Synthesis(
        summary=(
            "You stayed quiet in this one, and that is where nearly everyone starts — "
            "getting in is a skill, and it is the quickest one to build. There were "
            f"natural moments to come in.{moment} Next time, aim to say one sentence in the "
            "first two minutes; everything else in a group discussion grows from there."
        ),
        pattern="Not yet in the discussion: no turns were recorded, so there is nothing to "
        "score yet — only openings to take.",
        priority_improvement=(
            "Come in early with one short sentence. When someone asks the group a question, "
            'answer it first, even briefly: "I\'d say yes, mainly because…" gets you in, and '
            "the rest is easier once you have spoken."
        ),
        caveat="",
        focus_areas=[
            {
                "area": "Getting in",
                "what_happened": "The discussion moved on without you while openings came up."
                if openings
                else "The discussion moved on without you.",
                "why_it_matters": "A group discussion only sees what you say; one early "
                "sentence makes every later contribution easier.",
                "what_to_do": "Pick the first question put to the group and answer it in one "
                "sentence, before you feel fully ready.",
                "try_saying": "I'd say yes, mainly because…",
                "practice": "Start a short practice and use the moderator's opening invitation "
                "to say a single sentence.",
            }
        ],
        model="deterministic",
    )


def _openings_block(openings: list[str] | None) -> str:
    """Prompt text listing the moments others turned to this candidate (empty if none)."""
    # No openings means the section is omitted entirely.
    if not openings:
        return ""
    return (
        "\nOPENINGS (questions others put to the group — natural moments this candidate "
        "could have come in)\n" + "\n".join(openings) + "\n"
    )


def openings_for(candidate_label: str, metrics: CandidateMetrics, segments: list[Any]) -> list[str]:
    """For a quiet candidate only: the questions others asked, with timestamps."""
    # Anyone who spoke a reasonable amount isn't "quiet": no openings needed.
    if metrics.turns > 0 and metrics.participation_ratio > QUIET_SHARE:
        return []
    found: list[str] = []
    for segment in segments:
        text = (getattr(segment, "text", "") or "").strip()
        # An opening = someone else's line that asks a question.
        if getattr(segment, "candidate_id", None) == candidate_label or "?" not in text:
            continue
        start = float(getattr(segment, "start", 0.0))
        found.append(f"  {int(start) // 60}:{int(start) % 60:02d} {text[:MAX_LINE_CHARS]}")
        if len(found) >= MAX_OPENINGS:
            break
    return found


def own_lines_for(candidate_label: str, segments: list[Any]) -> list[str]:
    """The candidate's own transcript lines, timestamped, so advice can quote them."""
    lines = []
    # Chronological, non-empty lines by this candidate, each stamped mm:ss.
    for seg in sorted(segments, key=lambda s: s.start):
        if seg.candidate_id != candidate_label or not seg.text.strip():
            continue
        minutes, seconds = divmod(int(seg.start), 60)
        lines.append(f'  {minutes:02d}:{seconds:02d} "{seg.text.strip()[:MAX_LINE_CHARS]}"')
    # Too many lines: keep the start and end, drop the middle, to bound the prompt size.
    if len(lines) > MAX_OWN_LINES:
        lines = lines[: MAX_OWN_LINES // 2] + lines[-(MAX_OWN_LINES // 2) :]
    return lines


def _clean_items(raw: Any, keys: tuple[str, ...]) -> list[dict[str, str]]:
    """Keep well-formed items only; a malformed one is dropped, never fatal."""
    # Anything that isn't a list yields no items rather than an error.
    if not isinstance(raw, list):
        return []
    # Coerce values to strings; require the first key so empty shells are dropped.
    return [
        {key: str(item.get(key, "")) for key in keys}
        for item in raw
        if isinstance(item, dict) and item.get(keys[0])
    ]


async def synthesise(
    candidate_label: str,
    outputs: list[EvaluationOutput],
    weights: dict[str, float],
    metrics: CandidateMetrics,
    topic: str,
    segments: list[Any] | None = None,
) -> Synthesis:
    """Run the synthesis agent for one candidate. Never raises.

    A failure here costs the narrative and nothing else — the score, dimensions
    and evidence are already computed and stored (claude.md invariant 1).
    """
    # No dimension scores means there is nothing to explain.
    if not outputs:
        return Synthesis.unavailable("no dimension scores to synthesise")
    if metrics.turns == 0:
        # EC-14.1: told rule 14 in so many words, the model still opened with
        # "you contributed nothing, the score is very low" (2026-10-19). With no
        # lines of theirs to read there is nothing for it to analyse, so a silent
        # candidate's narrative is written here: kind, and about getting in.
        return silent_synthesis(openings_for(candidate_label, metrics, segments or []))

    provider = get_llm_provider()
    # Tools bound to this candidate's stored results, so numbers always come from code.
    tools = build_tools(candidate_label, outputs, weights)

    try:
        result = await provider.complete_with_tools(
            system=SYSTEM_PROMPT,
            prompt=build_prompt(
                candidate_label,
                outputs,
                metrics,
                topic,
                own_lines_for(candidate_label, segments or []),
                openings_for(candidate_label, metrics, segments or []),
            ),
            tools=tools,
            schema=RESPONSE_SCHEMA,
            max_tokens=SYNTHESIS_MAX_TOKENS,
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
    # Defensively convert the model's JSON into the typed container (and keep the
    # tool-call audit trail).
    return Synthesis(
        summary=str(data.get("summary", "")),
        pattern=str(data.get("pattern", "")),
        priority_improvement=str(data.get("priority_improvement", "")),
        caveat=str(data.get("caveat", "")),
        strengths=_clean_items(data.get("strengths"), ("point", "evidence", "why_it_helped")),
        focus_areas=_clean_items(
            data.get("focus_areas"),
            ("area", "what_happened", "why_it_matters", "what_to_do", "try_saying", "practice"),
        ),
        dimension_notes=_clean_items(
            data.get("dimension_notes"), ("dimension", "what_it_measures", "how_you_did")
        ),
        tool_calls=[
            {"tool": call.name, "arguments": call.arguments, "result": call.result}
            for call in result.tool_calls
        ],
        model=result.model,
    )
