"""The Observer Agent (FR-17, design §14.3, §15, §16).

One job: fold a new chunk into the running understanding of the discussion.

    previous rolling state + new chunk + latest metrics → updated rolling state

Three constraints from claude.md are enforced structurally here, not by asking
the model politely:

* **It never scores.** The prompt forbids it and the RollingState contract has
  no score field, so a model that tried could not produce valid output
  (invariant 7, design §16).
* **It never replaces the transcript.** The state is written to its own table;
  the raw segments are untouched (invariant 3).
* **It cannot break the session.** Every failure path returns the previous
  state and logs. A dead LLM means the rolling state stops updating and nothing
  else (invariant 1, design §38).
"""

import json
import logging
from dataclasses import dataclass

from services.common.contracts import (
    CandidateMetrics,
    ContractError,
    RollingState,
    load_schema,
    parse_rolling_state,
)
from services.llm import LLMError, get_llm_provider
from services.speech.chunking import Chunk

logger = logging.getLogger(__name__)

OBSERVER_PROMPT_VERSION = "1.0"

SYSTEM_PROMPT = """\
You are the Observer Agent in a group discussion evaluation system.

Your only job is to maintain a compressed, running understanding of a live \
discussion. You are NOT an evaluator.

Rules you must follow:
1. Do NOT score, rank, grade or rate any candidate. No numbers representing \
performance, no "strong"/"weak" verdicts. Early behaviour changes; judgement \
comes later, from a different agent, with the full transcript.
2. Produce OBSERVATIONS, not judgements. "Candidate C has spoken twice so far" \
is an observation. "Candidate C is underperforming" is a judgement — never \
write it.
3. Update the previous state rather than rewriting it. Carry forward anything \
still true; revise what the new chunk changed; drop nothing that still matters.
4. Attribute positions and interactions only to what was actually said in the \
transcript you are given.
5. Keep every list short and concrete. This state is a compression of the \
discussion, not a retelling of it.

Candidates are identified by short labels (A, B, C...). Use those labels."""


@dataclass(slots=True)
class ObserverUpdate:
    """Result of one loop iteration."""

    state: RollingState
    chunk_index: int
    changed: bool
    error: str | None = None


def _observation_schema() -> dict:
    """The RollingState contract with `session_id` removed.

    The model is not asked for the session id — it is not the model's to know,
    and trusting a returned one would be a way to write state onto the wrong
    session. `parse_rolling_state` puts the real one back.
    """
    schema = json.loads(json.dumps(load_schema("rolling_state")))
    schema["properties"].pop("session_id", None)
    schema["required"] = [key for key in schema.get("required", []) if key != "session_id"]
    schema.pop("$schema", None)
    schema.pop("$id", None)
    schema.pop("examples", None)
    return schema


def build_prompt(
    previous: RollingState,
    chunk: Chunk,
    metrics: dict[str, CandidateMetrics],
    topic: str,
) -> str:
    """Assemble the three inputs from design §14.3 into one prompt.

    Metrics are included so the Observer's observations about participation are
    grounded in measured numbers rather than its own impression of the text —
    the numbers are already computed deterministically, so letting the model
    re-estimate them would be strictly worse (claude.md invariant 4).
    """
    metric_lines = "\n".join(
        f"  {label}: {m.speaking_seconds:.0f}s speaking "
        f"({m.participation_ratio * 100:.0f}% of floor), {m.turns} turns, "
        f"{m.interruptions} interruptions, {m.questions} questions"
        for label, m in sorted(metrics.items())
    )
    previous_json = previous.model_dump_json(indent=2, exclude={"session_id"})

    return f"""\
DISCUSSION TOPIC
{topic}

PREVIOUS STATE (your understanding before this chunk)
{previous_json}

NEW TRANSCRIPT CHUNK ({chunk.start:.0f}s - {chunk.end:.0f}s)
{chunk.as_dialogue() or "(no speech in this window)"}

OBJECTIVE METRICS SO FAR (measured in code — treat as fact, do not re-estimate)
{metric_lines or "  (none yet)"}

Update the state to account for this chunk. Return the updated state only."""


async def observe(
    previous: RollingState,
    chunk: Chunk,
    metrics: dict[str, CandidateMetrics],
    topic: str,
    session_id: str,
) -> ObserverUpdate:
    """One iteration of the design §15 update loop.

    Never raises. On any failure the previous state is returned unchanged, so
    the loop can continue on the next chunk and the session never notices.
    """
    if chunk.is_empty:
        # Nothing was said in this window; there is nothing to fold in, and
        # asking the model to "update" from nothing invites invention.
        return ObserverUpdate(state=previous, chunk_index=chunk.index, changed=False)

    provider = get_llm_provider()
    prompt = build_prompt(previous, chunk, metrics, topic)

    try:
        result = await provider.complete_json(
            system=SYSTEM_PROMPT, prompt=prompt, schema=_observation_schema()
        )
        state = parse_rolling_state(result.data, session_id)
    except (LLMError, ContractError) as exc:
        logger.warning(
            "observer update failed; keeping previous state",
            extra={"session_id": session_id, "chunk_index": chunk.index, "error": str(exc)},
        )
        return ObserverUpdate(
            state=previous, chunk_index=chunk.index, changed=False, error=str(exc)
        )

    return ObserverUpdate(state=state, chunk_index=chunk.index, changed=True)
