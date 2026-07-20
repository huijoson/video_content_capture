"""Centralized, stable, documented CLI exit codes.

The spec recommends the following stable mapping (success 0; media 2;
configuration 3; provider auth 4; rate limit/transient exhausted 5; provider
payload/permanent provider 6; grounding 7; resume mismatch 8; filesystem 9;
interrupted 130; unexpected 1). The mapping is centralized here so the CLI
boundary converts accepted typed :class:`ErrorCategory` values to documented
nonzero exit codes without parsing exception messages.

The pipeline layer raises typed domain errors; the CLI boundary is the ONLY
place that translates them into process exit codes.
"""

from __future__ import annotations

import enum

from video_content_capture.domain.errors import ErrorCategory


class ExitCode(enum.IntEnum):
    """Stable, documented CLI exit codes."""

    SUCCESS = 0
    UNEXPECTED = 1
    MEDIA = 2
    CONFIGURATION = 3
    PROVIDER_AUTH = 4
    RATE_LIMIT = 5
    PROVIDER_PAYLOAD = 6
    GROUNDING = 7
    RESUME_MISMATCH = 8
    FILESYSTEM = 9
    INTERRUPTED = 130


# Stable mapping from typed error categories to exit codes. Kept explicit so
# the README (group 11) can document the exact table.
_CATEGORY_TO_EXIT: dict[ErrorCategory, ExitCode] = {
    ErrorCategory.MEDIA: ExitCode.MEDIA,
    ErrorCategory.CONFIGURATION: ExitCode.CONFIGURATION,
    ErrorCategory.PROVIDER_AUTH: ExitCode.PROVIDER_AUTH,
    ErrorCategory.RATE_LIMIT: ExitCode.RATE_LIMIT,
    ErrorCategory.PROVIDER_PAYLOAD: ExitCode.PROVIDER_PAYLOAD,
    ErrorCategory.GROUNDING: ExitCode.GROUNDING,
    ErrorCategory.RESUME_MISMATCH: ExitCode.RESUME_MISMATCH,
    ErrorCategory.FILESYSTEM: ExitCode.FILESYSTEM,
}


def exit_code_for_category(category: ErrorCategory) -> ExitCode:
    """Return the documented exit code for a typed error category."""

    return _CATEGORY_TO_EXIT.get(category, ExitCode.UNEXPECTED)


__all__ = ["ExitCode", "exit_code_for_category"]
