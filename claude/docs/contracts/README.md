# Contracts

These JSON Schemas are the **source of truth** for cross-component data shapes.
Do not improvise shapes in code. If a contract must change, update the schema
file here **first**, then update every producer and consumer.

Every LLM/agent output must validate against its schema before use — reject and
retry on malformed output.

| File | What it is | Produced by | Consumed by | Design ref |
|------|-----------|-------------|-------------|------------|
| `transcript_segment.schema.json` | One speaker-attributed, timestamped speech unit. Source of truth; persisted immediately. | Speech pipeline (STT + diarization) | Transcript store, metrics, observer, evaluators | §11, §13 |
| `candidate_metrics.schema.json` | Deterministic per-candidate metrics. Never LLM-estimated. | Metrics engine (code) | Behavior agent, aggregator | §17, §18 |
| `rolling_state.schema.json` | Compressed semantic memory; observations only, no final scores. | Observer Agent | Observer loop, Supervisor, final evaluation | §14.4, §16, §47 |
| `evaluation_job.schema.json` | Durable job that starts the agentic pipeline. Idempotent + versioned. | Session end / evaluation trigger | Evaluation queue, Supervisor | §20, §39, §40 |
| `evaluation_output.schema.json` | Per-agent, per-candidate, per-dimension structured result. | Specialized evaluator agents (LLM) | Score aggregator, report | §50, §31, §32 |

## Suggested usage

- **Python (FastAPI services):** mirror each schema as a Pydantic model and
  validate at every boundary; keep the JSON Schema as the canonical spec and
  the Pydantic model derived from it.
- **Validation gate:** wrap all agent calls so their raw output is validated
  against the matching schema before it flows downstream.
