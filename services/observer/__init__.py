"""
Purpose: Marks services/observer as a package: the Observer agent that keeps a rolling
    understanding of a live discussion, and the loop that drives it.
Why needed: Separates the live-understanding pipeline (C) from final evaluation (D): the
    Observer only produces observations, never scores (invariant 7).
Contains:
  - (no code) only this docstring; see agent.py and service.py.
Used by: services/evaluation/service.py, services/metrics/router.py,
    services/tests/test_observer.py
Depends on: nothing inside the project (standard library / third-party only)
"""
