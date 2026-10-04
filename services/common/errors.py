"""Domain errors and their HTTP mapping.

Services raise these; the API layer translates them once, in `services.main`,
so no business logic imports HTTP concepts.
"""


class DomainError(Exception):
    """Base class for expected, user-facing failures."""

    status_code = 400
    code = "domain_error"

    def __init__(self, message: str | None = None):
        super().__init__(message or self.__doc__ or self.code)
        self.message = message or self.code


class NotFoundError(DomainError):
    status_code = 404
    code = "not_found"


class ConflictError(DomainError):
    status_code = 409
    code = "conflict"


class AuthenticationError(DomainError):
    status_code = 401
    code = "authentication_failed"


class PermissionDeniedError(DomainError):
    status_code = 403
    code = "permission_denied"


class InvalidStateTransition(DomainError):
    """A state machine rejected the requested transition (design §8, §9, §45)."""

    status_code = 409
    code = "invalid_state_transition"
