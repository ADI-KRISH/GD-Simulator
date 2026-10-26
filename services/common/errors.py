"""Domain errors and their HTTP mapping.

Services raise these; the API layer translates them once, in `services.main`,
so no business logic imports HTTP concepts.

Purpose:    a small hierarchy of expected, user-facing exceptions, each carrying
            the HTTP status and machine-readable code the API should return.
Why needed: lets business code say "this was not found" or "that transition is
            illegal" without knowing about HTTP; `services.main` has one handler
            that turns any `DomainError` into a JSON response.
Contains:
    DomainError              - base class; defines status_code, code and message.
    NotFoundError            - 404, the record does not exist.
    ConflictError            - 409, clashes with current state (duplicates).
    AuthenticationError      - 401, bad/missing/expired credentials.
    PermissionDeniedError    - 403, signed in but not allowed.
    InvalidStateTransition   - 409, a state machine rejected a move (design
                               §8, §9, §45).
Used by:    auth/deps, auth/service, common/security, evaluation/router and
            service, main, notifications/router, practice/router, realtime/ws,
            session/router, service and state, simulation, speech/router, and
            the security / lifecycle / state-machine tests.
Depends on: nothing inside the project.
"""


class DomainError(Exception):
    """Base class for expected, user-facing failures."""

    # HTTP status and machine-readable code the API layer returns for this error.
    status_code = 400
    code = "domain_error"

    def __init__(self, message: str | None = None):
        # Message falls back to the class docstring, then to the code.
        super().__init__(message or self.__doc__ or self.code)
        self.message = message or self.code


class NotFoundError(DomainError):
    """The requested record does not exist (HTTP 404)."""

    status_code = 404
    code = "not_found"


class ConflictError(DomainError):
    """The request clashes with current state, e.g. a duplicate (HTTP 409)."""

    status_code = 409
    code = "conflict"


class AuthenticationError(DomainError):
    """Credentials or token are missing, wrong or expired (HTTP 401)."""

    status_code = 401
    code = "authentication_failed"


class PermissionDeniedError(DomainError):
    """The caller is signed in but not allowed to do this (HTTP 403)."""

    status_code = 403
    code = "permission_denied"


class InvalidStateTransition(DomainError):
    """A state machine rejected the requested transition (design §8, §9, §45)."""

    status_code = 409
    code = "invalid_state_transition"
