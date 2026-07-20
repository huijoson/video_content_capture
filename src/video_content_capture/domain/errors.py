"""Provider-neutral typed domain errors with stable categories.

Each error subclass exposes a stable :class:`ErrorCategory` value that later
groups (CLI exit-code mapping, retry classification) can branch on without
inspecting exception messages or provider-specific types. No provider SDK
types are imported here.
"""

from __future__ import annotations

import enum
from typing import Any


class ErrorCategory(enum.Enum):
    """Stable, provider-neutral error categories.

    The string values are intentionally stable so that later groups can map
    them to nonzero exit codes and retry decisions without parsing messages.
    """

    MEDIA = "media"
    CONFIGURATION = "configuration"
    PROVIDER_AUTH = "provider_auth"
    RATE_LIMIT = "rate_limit"
    PROVIDER_PAYLOAD = "provider_payload"
    GROUNDING = "grounding"
    RESUME_MISMATCH = "resume_mismatch"
    FILESYSTEM = "filesystem"


class DomainError(Exception):
    """Base class for all typed domain errors.

    Subclasses set :attr:`category` to a stable :class:`ErrorCategory`. The
    base class itself has no single category; callers should catch subclasses.
    """

    category: ErrorCategory | None = None

    def __init__(self, message: str, *, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.details: dict[str, Any] = dict(details) if details is not None else {}

    def __str__(self) -> str:
        return self.message


class MediaError(DomainError):
    """Media probing or audio extraction failed (missing file, no audio, ...)."""

    category = ErrorCategory.MEDIA


class ConfigurationError(DomainError):
    """Configuration is missing or invalid for the requested command."""

    category = ErrorCategory.CONFIGURATION


class ProviderAuthError(DomainError):
    """Provider authentication failed; not retried."""

    category = ErrorCategory.PROVIDER_AUTH


class RateLimitError(DomainError):
    """Provider rate-limited the request; eligible for bounded retry."""

    category = ErrorCategory.RATE_LIMIT


class ProviderPayloadError(DomainError):
    """Provider returned a malformed or unsupported payload; not retried."""

    category = ErrorCategory.PROVIDER_PAYLOAD


class GroundingError(DomainError):
    """Report evidence failed grounding validation (unknown IDs, empty claims)."""

    category = ErrorCategory.GROUNDING


class ResumeMismatchError(DomainError):
    """Stored resume state is incompatible with the current run configuration."""

    category = ErrorCategory.RESUME_MISMATCH


class FilesystemError(DomainError):
    """A filesystem operation (read/write/atomic replace) failed."""

    category = ErrorCategory.FILESYSTEM


__all__ = [
    "DomainError",
    "ErrorCategory",
    "FilesystemError",
    "GroundingError",
    "MediaError",
    "ConfigurationError",
    "ProviderAuthError",
    "ProviderPayloadError",
    "RateLimitError",
    "ResumeMismatchError",
]
