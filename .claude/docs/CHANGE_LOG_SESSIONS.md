# Session Changelog

A running, session-by-session record of this project: what was asked, errors and
failures (both user-reported and ones hit during the session), repairs made,
files changed, and state. Maintained by the `session-logger` subagent. Newest
entries at the top.

<!-- The session-logger appends new entries directly below this line. -->

## Session 1 — 2026-10-04  (example — delete once real entries exist)

**Goal(s):** Scaffold the project skeleton — FastAPI app, Postgres schema, JWT auth, health endpoint.

**Errors & failures — reported by user:**
- none reported

**Errors & failures — hit during session:**
- `alembic.util.exc.CommandError: Can't locate revision identified by 'head'` — first migration run before env was configured.

**Repairs & changes made:**
- Initialised Alembic properly and pointed `sqlalchemy.url` at the compose Postgres; migration then applied cleanly.

**Files changed:**
- `services/auth/main.py` — created: FastAPI app + `/health`.
- `infra/schema.sql` — edited: added users/profiles tables.

**Outcome / state:** Health endpoint responds; auth register/login working against local Postgres.

**Next step:** Start Slice 2 — lobby + session state machine.

---