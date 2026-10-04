# GD Platform — Project Context

Real-time, multi-participant Group Discussion platform with an Agentic AI
evaluation system. Multiple candidates enter a virtual room, discuss a topic
over audio/video, and are automatically evaluated on **what they say** and
**how they behave in the group**.

Full design lives in `@docs/design.md` (58 sections). Reference sections by
number when in doubt, e.g. "see design §14 (hybrid chunking)".

---

## Architecture invariants — NEVER violate these

These are the load-bearing decisions. If a change would break one of these,
stop and flag it instead of proceeding.

1. **Real-time and AI evaluation are DECOUPLED.** The live GD runs on its own
   path; AI evaluation is strictly downstream and asynchronous. An LLM / STT /
   evaluation-worker failure must **never** terminate or degrade a live GD
   session. (design §57 Principle 1, §38)

2. **Raw transcript is the source of truth.** Persist each transcript segment
   **immediately** as it is produced — never buffer until session end. It
   survives server/client/network/worker failure. (design §13, §57 Principle 2)

3. **Rolling state ≠ transcript.** The rolling state is a *compressed
   understanding* produced by the Observer Agent. It supplements, never
   replaces, the raw transcript. Final evaluation always uses BOTH raw
   transcript + rolling state + metrics. (design §47, §48)

4. **Metrics are DETERMINISTIC.** Speaking time, participation %, turn count,
   interruptions, response latency, question/argument counts — all computed in
   code from speech events, **never** estimated by an LLM. (design §17, §57
   Principle 8)

5. **Score aggregation is DETERMINISTIC.** The final score is a fixed weighted
   formula applied in code. LLMs emit only structured evidence + per-dimension
   scores; the aggregator applies the weighting policy. Never let an LLM output
   the final overall number directly. (design §31, §32, §57 Principle 11)
   The Synthesis Agent may *read* the score through a tool and explain it; the
   tool computes from stored data and takes no numeric argument, so this holds.

6. **Every score must be explainable.** Each dimension score carries supporting
   evidence (transcript timestamps + measurable behavior). No unexplained
   numbers. (design §57 Principle 12, NFR-08)

7. **The Observer Agent produces observations, not final scores.** During the
   live session it updates understanding only. It must never emit permanent
   per-candidate scores mid-session. (design §16)

8. **Candidates are evaluated in context, not in isolation.** Use
   candidate-specific evidence packets (their contributions + responses +
   responses received + interaction graph + metrics) layered on the shared GD
   context. (design §23, §27, §57 Principles 6 & 7)

---

## Stack

- **Frontend:** Next.js (React)
- **Backend APIs:** FastAPI (Python)
- **Real-time media:** WebRTC via LiveKit (media plane)
- **Real-time control:** WebSocket (application/control events)
- **Primary DB:** PostgreSQL (durable, strongly consistent)
- **Ephemeral state / cache:** Redis (lobbies, connected users, timers, session state)
- **Event bus:** Redis Streams for MVP → Kafka at scale
- **STT:** Whisper (chunked)
- **LLM layer:** LLM API (vendor-agnostic — keep behind an interface)
- **Object storage:** MinIO (S3-compatible) for audio/video/recordings/exports
- **Observability:** Prometheus + Grafana + structured logs

Keep the LLM, STT, and media-server integrations behind thin interfaces so
vendors can be swapped without touching business logic. The architecture must
stay vendor-independent. (design §54)

---

## Deployment — LOCAL ONLY

This project is **not deployed or hosted anywhere**. It runs entirely on the
developer's machine at `localhost`, brought up with `docker compose` from
`/infra` plus locally-run dev servers. Nothing in this repo may assume a public
URL, a cloud account, or a managed service.

- Do **not** add cloud provider SDKs, hosting configs, deploy pipelines,
  tunnels (ngrok/Cloudflare), or managed-service clients.
- Do **not** add k8s manifests, Terraform, or CI deploy jobs.
- Every service binds to `127.0.0.1` / `localhost`. Self-signed TLS only if a
  browser API forces it; plain HTTP otherwise.
- Secrets live in a local `.env` (gitignored) with dev-only values — never real
  cloud credentials.

Local port map (fixed — use these everywhere, don't invent new ones):

| Service | Local address |
|---|---|
| Frontend (Next.js dev) | http://localhost:3000 |
| Backend API (FastAPI) | http://localhost:8000 (OpenAPI at `/docs`) |
| WebSocket control plane | ws://localhost:8000/ws |
| LiveKit (self-hosted, Docker) | ws://localhost:7880 (RTC 7881/tcp, 7882/udp) |
| PostgreSQL | localhost:5442 |
| Redis (cache + Streams) | localhost:6389 |
| MinIO API / console | http://localhost:9010 / http://localhost:9011 |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3001 |

Postgres, Redis and MinIO sit one block off their default ports (5432 / 6379 /
9000-9001) because this machine already runs a native PostgreSQL service and
other projects' Redis and MinIO containers on the defaults. Inside the compose
network the containers still use standard ports; only the host mapping shifts.

Everything else about the design — the architecture, the invariants, the
agentic evaluation pipeline, the stack choices above — stays exactly as
specified. Local-only is a deployment constraint, not a design change.

The scaling material in design §42 and the production topology in §56 remain
the target architecture on paper, but nothing from them is built here: single
instance of each service, no load balancer, no horizontal scaling.

---

## Agent orchestration

- **Supervisor Agent** orchestrates evaluation — it plans, selects specialized
  agents based on GD type/rubric, builds context, dispatches, collects, resolves
  conflicts, and hands validated evidence to the aggregator. It does **not**
  score directly. (design §21, §22)
- **Specialized evaluator agents:** Content, Communication, Leadership,
  Collaboration, Critical Thinking, Behavior. Selected dynamically by the
  Supervisor per GD type. (design §22, §24–29)
- **Dispatch is batched per dimension by default** (`EVALUATION_STRATEGY`):
  one call per specialist covering every candidate, not one per
  (specialist, candidate) pair. 5 calls instead of 30 for a 6-person technical
  GD. This is design §49 ("avoid sending enormous duplicate prompts to every
  agent") and it is also better evaluation — leadership, dominance and
  participation balance are comparative, and a judge that can see the whole
  group can rank them. `per_candidate` remains available for comparison.
- **Final Synthesis Agent** (design §20 "Final Evaluator") writes the narrative
  layer of each candidate report *after* aggregation. It reads the score through
  **tools** (`compute_final_score`, `score_if`) that accept no numeric input and
  compute from stored data, so it can quote and explain the score, and quantify
  advice with true what-ifs, without being able to change it. Every tool call is
  recorded on the report for provenance.
- **Observer Agent** runs the live rolling-state loop (previous state + new
  chunk + metrics → updated state). (design §14–16)
- **Counsellor Agent** (personalization): takes GD context + candidate
  performance → contributions/responses/interactions analysis for personalized
  feedback. Maps to the Personalization Layer. **DEFERRED — not in MVP** unless
  explicitly scoped in. Confirm before building. (design §53; from handwritten notes)

> Orchestration approach: **LangGraph** (`services/evaluation/graph.py`), a
> compiled `StateGraph` with `Send` fan-out. Chosen over CrewAI/AutoGen
> because the roster is dynamic (design §22) and there is no inter-agent
> conversation to model. `EVALUATION_ENGINE=native` keeps the plain-asyncio
> path for comparison; both produce identical scores. Do not mix: new
> evaluation stages go in the graph. The Observer loop stays plain asyncio —
> it is a single repeated step, not a graph.

---

## Contracts (treat as hard interfaces)

All cross-component data uses fixed schemas in `@docs/contracts/`. Do not
improvise shapes — if a contract needs to change, update the contract file
first, then all consumers.

- **Transcript segment:** `{session_id, candidate_id, start, end, text, confidence}` (design §11)
- **Evaluation output (per agent):** `{candidate_id, dimension, score, confidence, strengths[], weaknesses[], evidence[{timestamp, summary}]}` (design §50)
- **Rolling state:** main_arguments, candidate_positions, recent_interactions, discussion_direction, unresolved_questions (design §14.4)

---

## Score weights (default — configurable by admin)

```
Content        30%
Communication  20%
Leadership     20%
Collaboration  20%
Behavior       10%
```
Final = 0.30·Content + 0.20·Comm + 0.20·Leadership + 0.20·Collab + 0.10·Behavior
(design §28, §31)

---

## State machines (implement as explicit enum + transition table; reject invalid transitions)

- **Session:** CREATED → WAITING → READY → STARTING → LIVE → ENDING → ENDED → EVALUATING → COMPLETED; any state → FAILED; FAILED → RETRYING → EVALUATING (design §8, §45)
- **Participant:** INVITED → JOINED → READY → ACTIVE → (DISCONNECTED ⇄ RECONNECTED) → ACTIVE → LEFT. A disconnect must NOT end the session. (design §9)
- **Evaluation:** QUEUED → PREPARING → RUNNING → WAITING_FOR_AGENT → AGGREGATING → REPORT_GENERATING → COMPLETED; → FAILED (design §45)

---

## Reliability rules

- **Idempotency:** evaluation jobs keyed by `session_id + evaluation_version`.
  Reprocessing a job must not create duplicate/inconsistent reports. (design §39)
- **Versioning:** rubric, prompt, and agent versions are recorded with every
  evaluation so any score is reproducible. (design §40)
- **Fault tolerance:** retries, durable queue, dead-letter queue, reconnection,
  partial-failure isolation. STT failure → retry + keep raw audio + reprocess
  segment. (design §38, NFR-04)
- **Consistency:** durable session state & final results are strongly
  consistent; ephemeral real-time state may be eventually consistent. (NFR-05)

---

## Repo layout (target)

```
/frontend            Next.js app (all pages built)
/services
  /common            config, db, errors, events (Redis Streams), contracts,
                     security, storage, logging, timeutil
  /auth              FR-01, FR-02
  /lobby             §7.4 — membership, readiness (a view over session)
  /session           §7.5, state machines, timer, topic delivery
  /realtime          WebSocket control plane, hub, timer ticker, LiveKit tokens
  /speech            audio ingestion → chunk → STT → speaker mapping → transcript
  /metrics           deterministic metrics engine + interaction graph (§17, §30)
  /observer          rolling-state loop (§14–16)
  /evaluation        graph (LangGraph), queue, supervisor, /agents,
                     aggregator, synthesis, report, worker
  /notifications     durable notifications (FR-32)
  /llm               vendor-agnostic LLM interface (JSON + tool use) with
                     Anthropic, OpenAI and stub adapters
  /migrations        alembic (0001 auth, 0002 session, 0003 speech+evaluation,
                     0004 notifications)
  /tests             301 tests, no Docker/Redis/model/API key required
/claude
  claude.md          this file
  /docs/design.md    full spec
  /docs/contracts    JSON schemas (source of truth for shapes)
/infra               docker-compose (local stack), livekit/prometheus/grafana
```

`/infra/docker-compose.yml` is the whole "deployment": Postgres, Redis,
LiveKit, MinIO, Prometheus, Grafana — all published on the localhost ports
above. Frontend and backend run as local dev servers against it.

---

## Conventions

- Persist transcript segments the moment they exist — no end-of-session batch writes.
- Prefer deterministic code over an LLM call whenever a value is directly measurable.
- Every LLM/agent output must validate against its contract before use; reject/retry on malformed output.
- Emit domain events to the bus (design §36 event list) rather than calling downstream services directly.
- Write tests that Claude Code actually runs: state-machine transitions, metrics math, aggregator math, and contract validation are all deterministic and must be unit-tested.
- Pin dependency versions. For LiveKit specifically, verify against current docs rather than memory — its API changes frequently.

---

## Working agreement for Claude Code

- Build in **vertical slices** (auth → lobby/session → realtime → speech →
  metrics+observer → evaluation). One slice at a time. Don't touch adjacent
  slices unless asked.
- For any non-trivial slice, produce a **plan / file list first**, wait for
  review, then implement.
- After writing code, **run it locally** (tests, migrations, health check
  against the localhost ports above) and fix failures before declaring done.
  "Done" means it works on this machine — there is no deploy step.
- If a request conflicts with an architecture invariant above, **stop and say
  so** rather than silently working around it.
- Keep the MVP scope (design §55) unless told otherwise: single Observer, single
  final judge path, Redis Streams (not Kafka), no vector DB.

## Decisions — locked

- [x] **Agent orchestration: LangGraph.** See "Agent orchestration" above.
      `services/evaluation/graph.py` is the workflow; `supervisor.py` still
      owns planning and conflict resolution, called from the graph's nodes.
- [x] **LiveKit hosting: self-hosted locally in Docker** (`ws://localhost:7880`,
      dev API key/secret from `.env`). LiveKit Cloud is out of scope. Tokens are
      minted directly with PyJWT in `services/realtime/livekit.py` rather than
      via the vendor SDK, keeping the media integration one thin file.
- [x] **Counsellor Agent / personalization: DEFERRED**, per the MVP scope in
      design §55. The inputs it would need are already collected and exposed —
      `GET /api/v1/me/history` returns per-dimension averages across sessions —
      so adding the layer later needs no schema change.
- [x] **LLM provider: pluggable behind `services/llm/`**, selected by
      `LLM_PROVIDER` in `.env`. Three adapters ship: `anthropic` (default model
      `claude-opus-5`), `openai` (e.g. `gpt-4o-mini`), and `stub` — a
      deterministic offline provider, and the default in a fresh checkout, so
      the whole agentic pipeline runs and is testable with no API key.
      Structured output differs by vendor and the adapters absorb it: Anthropic
      uses `output_config.format`, OpenAI uses `response_format` with a JSON
      schema plus a strict-subset check (`evaluation_output` qualifies,
      `rolling_state` does not). Every response passes the contract validation
      gate regardless of provider.
- [x] **STT provider: Whisper locally via `faster-whisper`**, behind
      `services/speech/stt.py`. `STT_PROVIDER=auto` (the default) uses Whisper
      when installed and falls back to a deterministic stub otherwise, so the
      pipeline runs before the ~1 GB model download. Install
      `services/requirements-speech.txt` for real transcription.
- [x] **Object storage: MinIO in Docker**, behind `services/common/storage.py`.
      Defaults to `OBJECT_STORE=filesystem` (`services/media/`, gitignored) so
      audio ingestion works with nothing else running.

### Aggregation policy decisions (design §31 leaves these open)

- **Critical thinking has no weight of its own.** Design §22 selects a Critical
  Thinking agent for technical and HR/social GDs, but the §31 rubric has five
  weighted dimensions. It is therefore scored under the `content` weight:
  averaged with content when both ran, standing in for content when only it ran.
  One place to change it: `DIMENSION_ALIASES` in `evaluation/aggregator.py`.
- **A missing dimension renormalises rather than scoring zero.** If an evaluator
  fails, its weight is redistributed across the dimensions that did score. A
  failed agent is the system's problem, not the candidate's — the result is
  flagged `needs_human_review` instead (design §51, §52).