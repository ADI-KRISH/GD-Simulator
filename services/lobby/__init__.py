"""
Purpose: Marks services/lobby as a package. The lobby has no tables or rules of its own; it is a
    view over the session slice.
Why needed: Keeps the lobby HTTP routes in their own import path (services.lobby.router) while
    the data and rules stay in services/session, so membership lives in exactly one place.
Contains:
  - (no code) only this docstring; see router.py for the endpoints.
Used by: nothing imports it directly (the package is imported implicitly by
    services.lobby.router)
Depends on: nothing inside the project (standard library / third-party only)
"""
