"""Domain errors.

They carry a stable machine readable ``code`` so the HTTP layer can translate them into
consistent JSON error bodies without leaking Python tracebacks.
"""

from __future__ import annotations

__all__ = [
    "ConflictError",
    "DomainError",
    "ForbiddenError",
    "InvalidRuleError",
    "NotFoundError",
    "RateLimitedError",
    "SyncRejectedError",
    "UnauthorizedError",
    "ValidationError",
]


class DomainError(Exception):
    """Base class for every expected business failure."""

    code = "domain_error"
    http_status = 400

    def __init__(self, message: str, *, details: dict[str, object] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details = details or {}


class NotFoundError(DomainError):
    code = "not_found"
    http_status = 404


class UnauthorizedError(DomainError):
    code = "unauthorized"
    http_status = 401


class ForbiddenError(DomainError):
    code = "forbidden"
    http_status = 403


class ConflictError(DomainError):
    code = "conflict"
    http_status = 409


class ValidationError(DomainError):
    code = "validation_error"
    http_status = 422


class RateLimitedError(DomainError):
    code = "rate_limited"
    http_status = 429


class InvalidRuleError(DomainError):
    code = "invalid_rule"
    http_status = 422


class SyncRejectedError(DomainError):
    code = "sync_rejected"
    http_status = 409
