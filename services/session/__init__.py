"""
Purpose: Marks services/session as a package: session and participant tables, the state
    machines, the business rules and the session HTTP API.
Why needed: Everything about the life of a group discussion (create, lobby, start, live, end) is
    imported from services.session.*; the lobby, realtime, speech, metrics and evaluation slices
    all build on it.
Contains:
  - (no code) only this docstring; see state.py, models.py, repository.py, service.py,
    schemas.py and router.py.
Used by: services/evaluation/router.py, services/evaluation/service.py,
    services/evaluation/worker.py, services/lobby/router.py, services/metrics/router.py,
    services/metrics/service.py, services/observer/service.py, services/practice/engine.py,
    services/practice/router.py, services/realtime/timer.py, services/realtime/ws.py,
    services/session/router.py, services/session/service.py, services/simulation.py,
    services/speech/router.py, services/tests/conftest.py,
    services/tests/test_evaluation_api.py, services/tests/test_evaluation_pipeline.py,
    services/tests/test_multiplayer_rules.py, services/tests/test_notifications.py,
    services/tests/test_observer.py, services/tests/test_practice.py,
    services/tests/test_session_lifecycle.py
Depends on: nothing inside the project (standard library / third-party only)
"""
