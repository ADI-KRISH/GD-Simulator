"""
Purpose: Marks services/metrics as a package: the deterministic metrics engine, its service and
    its HTTP API.
Why needed: Keeps every measured quantity (speaking time, turns, interruptions, latency) in one
    slice that never calls an LLM, as claude.md invariant 4 requires.
Contains:
  - (no code) only this docstring; see engine.py, service.py and router.py.
Used by: services/evaluation/service.py, services/metrics/router.py,
    services/observer/service.py
Depends on: nothing inside the project (standard library / third-party only)
"""
