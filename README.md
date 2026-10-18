# GD Platform

A real-time, multi-participant **Group Discussion platform with an agentic AI
evaluation system**. Candidates join a virtual room, discuss a topic over
audio/video, and are automatically evaluated on **what they say** and **how they
behave in the group** — with every score traceable to a transcript timestamp or
a measured quantity.

The full specification is `claude/docs/design.md` (58 sections); the working
context and architecture invariants are in `claude/claude.md`.

> **This project is not deployed anywhere.** It runs entirely on one machine at
> `localhost`, brought up with `docker compose` plus two local dev servers.

---

## The architecture in one page

Four pipelines run in parallel. The important property is that the first is
**decoupled** from the rest: an LLM, STT or evaluation failure can never
terminate or degrade a live discussion.

```
A. Live discussion        browser ──WebRTC──> LiveKit ──> other participants
                          browser ──WebSocket──> control plane (timer, roster)

B. Speech processing      mic ─> 45s windows ─> object store ─> Whisper
                              ─> speaker mapping ─> transcript segments (Postgres)

C. Live understanding     chunk + previous state + metrics ─> Observer Agent
                              ─> rolling state (observations only, never scores)

D. Final evaluation       transcript + rolling state + metrics
                              ─> Supervisor ─> specialist agents (one call each,
                                 covering every candidate)
                              ─> deterministic aggregator (code)
                              ─> Synthesis agent + scoring tools ─> report
```

Three things are deliberately **not** done by a language model:

| Concern | Why it is code, not an LLM |
|---|---|
| Speaking time, turns, interruptions, latency | Directly measurable. Estimating them would be less accurate and non-reproducible. |
| The final score | A fixed weighted formula over per-dimension scores, so a score is reproducible and comparable across sessions. |
| Speaker attribution | Each browser records only its own microphone, so attribution is exact by construction rather than inferred from a mixed stream. |

---

## Running it

**One step:** double-click `start.bat` (or run `.\start.ps1`). It starts Docker
Desktop if needed, brings up the containers, runs migrations, rebuilds the
frontend only if its source changed, opens the backend and frontend in their own
windows, waits until both answer, and opens http://localhost:3000. Safe to re-run.

The manual steps below are what it automates.

### 1. Infrastructure

```bash
cd infra
docker compose --env-file ../.env up -d
```

Brings up Postgres, Redis, LiveKit, MinIO, Prometheus and Grafana. Every port is
bound to `127.0.0.1` and offset off the defaults, because this machine already
runs other projects' Postgres, Redis and MinIO.

| Service | Address |
|---|---|
| Frontend (Next.js) | http://localhost:3000 |
| Backend API | http://localhost:8000 — OpenAPI at `/docs` |
| WebSocket control plane | ws://localhost:8000/ws/sessions/{id} |
| LiveKit | ws://localhost:7880 |
| PostgreSQL | localhost:5442 |
| Redis | localhost:6389 |
| MinIO API / console | http://localhost:9010 / http://localhost:9011 |
| Prometheus | http://localhost:9090 |
| Grafana | http://localhost:3001 |

### 2. Backend

```bash
cp .env.example .env                 # first run only
python -m venv .venv
./.venv/Scripts/python.exe -m pip install -r services/requirements-dev.txt
./.venv/Scripts/alembic.exe upgrade head
./.venv/Scripts/python.exe -m uvicorn services.main:app --host 127.0.0.1 --port 8000
```

Check it: `curl http://localhost:8000/health` →
`{"status":"ok","checks":{"database":true,"redis":true}}`.

Three background loops start with the app: the session timer ticker, the
Observer loop, and the evaluation worker. Each contains its own failures — a
stalled loop degrades one capability rather than the process.

### 3. Frontend

```bash
cd frontend
npm install
npm run dev
```

### 4. Tests

```bash
./.venv/Scripts/python.exe -m pytest -q      # 301 tests
./.venv/Scripts/python.exe -m ruff check services/
cd frontend && npx tsc --noEmit && npm run build
```

The Python suite runs against in-memory SQLite with stubbed STT, LLM and object
storage — **no Docker, no Redis, no model download and no API key required**.

---

### Without Docker (AI-agent demo)

The AI-agent demo needs none of the containers. SQLite replaces Postgres,
`REDIS_URL=disabled` makes every Redis call fail instantly (events are dropped
and the evaluation worker runs jobs straight from the database), recordings use
the local filesystem, and agents publish no media so LiveKit is unused.

```powershell
# Backend (PowerShell, repo root)
$env:DATABASE_URL = "sqlite+aiosqlite:///./gd_local.db"
$env:REDIS_URL = "disabled"
.\.venv\Scripts\alembic.exe upgrade head
.\.venv\Scripts\python.exe -m uvicorn services.main:app --host 127.0.0.1 --port 8000

# Frontend (second window)
cd frontend; npm run build; npm start
```

`/health` reports `ok` with `"redis": null` — switched off by configuration is
not a fault. What does not work without Docker: real audio/video between people
(LiveKit). Transcription and evaluation still work.

### As part of the AI Interviewer platform

`start_gd.bat` runs GD the platform's way: API on **8114**, web app on **5214**
(PORTS.txt), no Docker, each in a tab of the platform's terminal window. The
platform's own `start.bat` (Platform_Shell) calls it, and its `stop` closes it.

- **Sign-in is the platform's.** The shell's `/gd` page frames GD's `/platform`
  page, which trades the platform's identity (Platform_Core `ai_core.identity`)
  for GD's own tokens — `POST /api/v1/auth/platform`, on only with
  `PLATFORM_BRIDGE=1`. Inside the frame GD drops its own brand and sign-out.
- **Results reach the skill graph.** After each report, a platform student's
  scores go to M04 as `gd_contribution` observations, quoting their own lines,
  if they have practice consent there. Four dimensions map to M04's
  communication concepts (`services/ai_platform/skill_graph.py`); content and
  leadership have no honest match and stay in GD's report. M04 records the
  evidence without moving its ability estimate (its rule EC-4.17).
- **Model key:** the first `GROQ_API_KEYS` entry in `keys.txt` beside the repos
  — one Groq organization per running app. Read into the API's process only.

## Optional: real STT and real evaluation

Both integrations sit behind thin interfaces and default to deterministic
offline stubs, so a fresh checkout runs the entire pipeline immediately. The
stubs are visibly synthetic — they exist to exercise the plumbing, not to
pretend to be judgements.

**Real transcription** (FR-13):

```bash
./.venv/Scripts/python.exe -m pip install -r services/requirements-speech.txt
# .env:  STT_PROVIDER=whisper   (or leave `auto`, which prefers Whisper)
```

**Real evaluation** (FR-20 – FR-30). Two adapters ship; pick one in `.env`:

```bash
./.venv/Scripts/python.exe -m pip install anthropic     # or: openai
```

```ini
# Anthropic
LLM_PROVIDER=anthropic
LLM_MODEL=claude-opus-5
LLM_API_KEY=sk-ant-...

# OpenAI
LLM_PROVIDER=openai
LLM_MODEL=gpt-4o-mini
LLM_API_KEY=sk-...
```

Nothing else changes — no agent, prompt, aggregator or contract differs between
them. That is the point: `services/speech/stt.py` and `services/llm/` are the
only files in the project that know a vendor exists, and having two LLM adapters
is what makes the vendor-independence requirement (design §54) testable rather
than aspirational.

The two providers reach structured output differently, and the adapters absorb
that: Anthropic takes `output_config.format`, OpenAI takes `response_format`
with a JSON schema, and OpenAI's *strict* mode accepts only a subset of JSON
Schema (every property required, no open objects or maps). The adapter detects
which contracts qualify — `evaluation_output` does, `rolling_state` does not —
and sends each accordingly. Either way the response passes the same contract
validation gate before anything downstream sees it.

---

## Walking through a discussion

1. **Register** a host account and some candidate accounts.
2. **Host creates a GD** (topic, type, duration, capacity). The GD *type* drives
   which evaluator agents run (design §22), so it is a real choice, not a label.
3. **Candidates join** with the six-character code and mark themselves ready.
   Nothing starts until the minimum is present and everyone is ready.
4. **Host starts.** Preparation runs, then the discussion clock. The topic is
   released only at that point, so nobody prepares ahead of the others.
5. **They talk.** Audio and video flow through LiveKit; the control plane
   broadcasts the timer and roster; each browser uploads 45-second windows of
   its own microphone for transcription.
6. **Time runs out** (or the host ends it). The session moves
   `LIVE → ENDING → ENDED`, and an evaluation job is enqueued.
7. **Evaluation runs downstream.** The Supervisor selects agents by GD type,
   builds one evidence packet per candidate, and dispatches **one call per
   specialist covering all candidates** — 5 calls for a 6-person technical GD
   rather than 30. Validated results go to the deterministic aggregator, and a
   Synthesis agent then writes each candidate's narrative, reading the score
   through tools it cannot use to change it.
8. **Reports appear.** Each candidate sees their own; the host also sees the
   group report and every score. Everyone is notified, and the notification
   survives being offline — it is a row, not just a broadcast.

A host can also **invite** a registered candidate by email, which reserves a
seat and a candidate label and notifies them. They still join through the normal
code path: the invitation is a reservation and a pointer, not a second way in.
An unanswered invitation never blocks the start.

---

## Two decisions worth knowing

**The evaluation workflow is a LangGraph state graph.**
`services/evaluation/graph.py` compiles `plan → evaluate (dynamic fan-out) →
collect → aggregate → synthesise`, with a conditional edge to `END` when
every specialist fails. `render_mermaid()` emits the real topology from the
compiled graph. The `aggregate` node runs no model — adopting a framework did
not move the determinism boundary. `EVALUATION_ENGINE=native` runs the same
stages under plain asyncio; both produce identical scores.

**Batched dispatch.** `EVALUATION_STRATEGY=per_dimension` (the default) sends
each specialist one call covering every candidate; `per_candidate` sends one per
pair. Both produce identical downstream data, so the same session can be run
both ways and compared. Batching is cheaper *and* better for comparative
dimensions — but it risks turning absolute scoring into relative ranking, so the
system prompt says explicitly that 5 means "average for this dimension", not
"average for this group", and the response is checked for duplicated or skipped
candidates.

**The synthesis agent has tools, not arithmetic.** The final number stays in
code (claude.md invariant 5). The agent that writes the narrative calls
`compute_final_score()` — which takes no arguments and reads the stored
evaluation — and `score_if(dimension, hypothetical_score)`, which re-runs the
same aggregator on a hypothetical and stores nothing. So the narrative can say
"collaboration at 8.0 instead of 5.2 would have put you at 79" and be
arithmetically true, while remaining unable to move the real score. Every tool
call is recorded on the report.

## Contracts

`claude/docs/contracts/*.schema.json` are the source of truth for every
cross-component shape. They are mirrored as Pydantic models in
`services/common/contracts.py`, and validated *against the schema files
themselves* at runtime — so a model and its contract cannot drift apart
silently. Every agent response passes through that gate before anything
downstream sees it.

| Contract | Produced by | Consumed by |
|---|---|---|
| `transcript_segment` | speech pipeline | metrics, observer, evaluators |
| `candidate_metrics` | metrics engine (code) | behaviour agent, aggregator |
| `rolling_state` | Observer Agent | supervisor, final evaluation |
| `evaluation_job` | session end | queue, supervisor |
| `evaluation_output` | evaluator agents | aggregator, report |

---

## Reliability

| Failure | What happens |
|---|---|
| LLM unavailable | The discussion is unaffected. Evaluation waits in the queue and retries; the Observer keeps the previous state. |
| STT fails on a chunk | The chunk is marked failed, the raw audio is retained, and `POST /transcript/reprocess` replays it. |
| A candidate disconnects | Marked `DISCONNECTED`, the session continues, and they can reconnect. |
| An evaluator agent fails | Isolated. Its weight is redistributed and the result is flagged for human review — the candidate is not scored zero for it. |
| An evaluation job is delivered twice | The `(session_id, evaluation_version)` unique constraint makes it converge on one report. |
| Redis is down | Domain events are dropped with a warning; durable truth is already in Postgres and a reconciliation sweep re-enqueues lost jobs. |
| The backend restarts mid-session | The clock is derived from stored timestamps, not counted in memory, so it resumes correctly. |

---

## Scope

Built to the MVP scope in design §55: single Observer, single evaluation path,
Redis Streams (not Kafka), no vector database, one instance of everything.

The Counsellor Agent / personalization layer (design §53) is **deferred** — the
data it needs is already collected and exposed at `GET /api/v1/me/history`.

The scaling material in design §42 and the production topology in §56 remain
the target architecture on paper; none of it is built here. That is a
deployment constraint, not a design change.
