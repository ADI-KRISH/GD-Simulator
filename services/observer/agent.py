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

Purpose: The Observer agent: given the previous rolling state, a new 45-second transcript chunk
    and the latest metrics, it asks the LLM for an updated rolling state and validates the
    answer.
Why needed: It keeps a compressed running picture of the discussion (main arguments, positions,
    interactions, open questions) that final evaluation can read alongside the raw transcript,
    without ever scoring anyone mid-session. Every failure returns the previous state, so a dead
    LLM never touches the live session.
Contains:
  - OBSERVER_PROMPT_VERSION: bump when the prompt changes so results stay reproducible (design
    §40).
  - SYSTEM_PROMPT: instructions that forbid scoring and demand observations only.
  - ObserverUpdate: result of one step (new state, chunk index, changed flag, error).
  - _observation_schema: the RollingState contract minus session_id, which the model must not
    supply.
  - build_prompt: assemble topic, previous state, new chunk and measured metrics into one
    prompt.
  - observe: run one update step; never raises and falls back to the previous state.
Used by: services/observer/service.py, services/tests/test_llm_providers.py,
    services/tests/test_observer.py
Depends on: services/common/contracts.py, services/llm/__init__.py, services/speech/chunking.py
"""

import json  # deep-copy the schema via a JSON round trip
import logging  # warn when an update fails
from dataclasses import dataclass  # ObserverUpdate container

from services.common.contracts import (  # contract models and validation gate
    CandidateMetrics,
    ContractError,
    RollingState,
    load_schema,
    parse_rolling_state,
)
from services.llm import LLMError, get_llm_provider  # provider-agnostic LLM access
from services.speech.chunking import Chunk  # a 45-second transcript window

logger = logging.getLogger(__name__)

# Bump when SYSTEM_PROMPT or build_prompt changes so results stay reproducible (design §40).
OBSERVER_PROMPT_VERSION = "1.0"

# Fixed instructions: observe and compress, never judge or score.
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
    # Deep-copy through JSON so editing the schema never mutates the shared cached contract.
    schema = json.loads(json.dumps(load_schema("rolling_state")))
    # Drop session_id: the model must not supply it (see the docstring).
    schema["properties"].pop("session_id", None)
    # Also remove it from the required list, otherwise validation would demand it.
    schema["required"] = [key for key in schema.get("required", []) if key != "session_id"]
    # Strip metadata keys that are not part of the shape the model must produce.
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
    # One compact line of measured numbers per candidate, sorted by label.
    metric_lines = "\n".join(
        f"  {label}: {m.speaking_seconds:.0f}s speaking "
        f"({m.participation_ratio * 100:.0f}% of floor), {m.turns} turns, "
        f"{m.interruptions} interruptions, {m.questions} questions"
        for label, m in sorted(metrics.items())
    )
    # The previous state without session_id, since the model never sees or returns it.
    previous_json = previous.model_dump_json(indent=2, exclude={"session_id"})

    # The prompt carries the topic, the previous state, the new chunk and the measured metrics
    # (design §14.3).
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

    # Whichever LLM is configured; the offline stub works too.
    provider = get_llm_provider()
    prompt = build_prompt(previous, chunk, metrics, topic)

    # Ask for JSON matching the contract, then validate it before trusting it.
    try:
        result = await provider.complete_json(
            system=SYSTEM_PROMPT, prompt=prompt, schema=_observation_schema()
        )
        # Re-attaches the real session id and rejects malformed output.
        state = parse_rolling_state(result.data, session_id)
    # Either failure keeps the previous state so the session is never affected.
    except (LLMError, ContractError) as exc:
        logger.warning(
            "observer update failed; keeping previous state",
            extra={"session_id": session_id, "chunk_index": chunk.index, "error": str(exc)},
        )
        return ObserverUpdate(
            state=previous, chunk_index=chunk.index, changed=False, error=str(exc)
        )

    # Success: the new state replaces the old one.
    return ObserverUpdate(state=state, chunk_index=chunk.index, changed=True)
