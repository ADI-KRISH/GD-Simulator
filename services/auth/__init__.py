"""Auth + user profile service (FR-01, FR-02).

Purpose: Marks services/auth as a Python package and states what the slice covers: accounts,
    login, tokens and user profiles.
Why needed: Without the package marker the routers, tests and migrations could not import
    services.auth.*; the docstring tells a new reader where FR-01 (accounts) and FR-02
    (profiles) live.
Contains:
  - (no code) only this docstring; the real code is in models, schemas, repository, service,
    router and deps.
Used by: services/ai_platform/bridge.py, services/auth/deps.py, services/auth/router.py,
    services/auth/service.py, services/evaluation/service.py, services/migrations/env.py,
    services/practice/router.py, services/realtime/ws.py, services/tests/conftest.py
Depends on: nothing inside the project (standard library / third-party only)
"""
