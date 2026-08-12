class DomainError(ValueError):
    """Raised when a domain invariant is violated."""


class NotFoundError(DomainError):
    """Raised when a referenced aggregate does not exist."""


class ConflictError(DomainError):
    """Raised when a write conflicts with existing canonical identity."""


class AuthorizationError(DomainError):
    """Raised when an actor is not allowed to perform a governed transition."""

