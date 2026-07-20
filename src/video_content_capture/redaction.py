"""Reusable credential redaction for CLI output, logging, and error text.

Every configured credential value MUST be scrubbed from:

* CLI output (stdout/stderr) and Rich progress,
* logging messages,
* exception text / details / ``repr``,
* generated metadata and manifest artifacts.

This module provides:

* :func:`scrub_text` — replace every secret value in a string with a stable
  ``[REDACTED]`` placeholder. Used at the CLI boundary before printing
  exception messages or details.
* :class:`RedactingFilter` — a :class:`logging.Filter` that scrubs configured
  secret values from every log record's message and (best-effort) from
  formatted exception info. Attach it to a handler to keep secrets out of
  logs without changing call sites.
* :func:`install_redaction` — wire the filter onto the root handlers for the
  ``vcc`` logger so the CLI's logging configuration redacts by default.

The redactor never logs the secret list itself and never raises on weird
inputs (a ``None`` message is treated as empty). Secrets are matched by exact
substring replacement, longest-first, so a secret that is a prefix of another
is replaced before the shorter one. Secrets that are empty or whitespace are
ignored (they would otherwise scrub every empty substring).

This module does NOT import Typer or Rich; it is reusable from the pipeline
and CLI layers alike.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable

# Stable placeholder shown wherever a secret value would have appeared.
_REDACTED = "[REDACTED]"


def _clean_secrets(secrets: Iterable[str]) -> list[str]:
    """Return non-empty, deduplicated secrets ordered by descending length."""

    seen: set[str] = set()
    out: list[str] = []
    for s in secrets:
        if s is None:
            continue
        s = str(s)
        if s.strip() == "":
            continue
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    # Longest-first so a longer secret that contains a shorter one is replaced
    # before the shorter substring could match.
    out.sort(key=len, reverse=True)
    return out


def scrub_text(text: str | None, secrets: Iterable[str] | None = None) -> str:
    """Return ``text`` with every secret value replaced by ``[REDACTED]``.

    ``secrets`` defaults to the process-wide configured secrets registered via
    :func:`register_secrets`. The replacement is exact substring, longest
    first. ``None`` text becomes ``""``.
    """

    if text is None:
        return ""
    result = str(text)
    active = _clean_secrets(secrets) if secrets is not None else _active_secrets()
    for secret in active:
        if secret and secret in result:
            result = result.replace(secret, _REDACTED)
    return result


# --- Process-wide secret registry -----------------------------------------
#
# The CLI registers configured secret values once after building Settings so
# the logging filter and error scrubber can redact without threading the
# secret list through every call site. The registry holds only the secret
# string values (never their names) and is module-private.


_active: list[str] = []


def register_secrets(secrets: Iterable[str]) -> None:
    """Register configured secret values for process-wide redaction.

    Idempotent: re-registration replaces the active set (so a test that
    reconfigures Settings does not accumulate stale secrets).
    """

    global _active
    _active = _clean_secrets(secrets)


def _active_secrets() -> list[str]:
    return list(_active)


def clear_secrets() -> None:
    """Clear the process-wide secret registry (test helper)."""

    global _active
    _active = []


def scrub_exception(exc: BaseException, secrets: Iterable[str] | None = None) -> str:
    """Return a redacted, human-readable rendering of ``exc``.

    Includes ``str(exc)`` and, for :class:`DomainError` subclasses, a redacted
    rendering of the ``details`` dict so structured error details do not leak
    secrets either. Never raises.
    """

    parts: list[str] = [scrub_text(str(exc), secrets)]
    # DomainError carries a structured ``details`` dict; redact its values.
    details = getattr(exc, "details", None)
    if details:
        try:
            import json

            rendered = json.dumps(details, ensure_ascii=False, default=str)
            parts.append(scrub_text(rendered, secrets))
        except Exception:  # pragma: no cover — defensive, never raise
            pass
    # Redact the repr as well (some exceptions include args in repr).
    try:
        parts.append(scrub_text(repr(exc), secrets))
    except Exception:  # pragma: no cover — defensive
        pass
    return " ".join(p for p in parts if p)


# --- Logging filter --------------------------------------------------------


class RedactingFilter(logging.Filter):
    """A :class:`logging.Filter` that scrubs configured secrets from records.

    Attach to a handler: ``handler.addFilter(RedactingFilter(secrets=[...]))``.
    The secret list may be omitted to use the process-wide registry populated
    via :func:`register_secrets`.
    """

    def __init__(self, secrets: Iterable[str] | None = None) -> None:
        super().__init__()
        self._explicit: list[str] | None = _clean_secrets(secrets) if secrets is not None else None

    def _secrets(self) -> list[str]:
        return self._explicit if self._explicit is not None else _active_secrets()

    def filter(self, record: logging.LogRecord) -> bool:  # noqa: A003
        # Scrub the formatted message.
        record.msg = scrub_text(record.getMessage(), self._secrets())
        record.args = ()  # already folded into msg via getMessage
        # Best-effort: scrub exc_info text if present.
        if record.exc_info:
            try:
                import traceback

                tb_text = "".join(traceback.format_exception(*record.exc_info))
                # We cannot rewrite the live exc_info, but we can append a
                # redacted rendering to the message so the handler emits scrubbed
                # text. The raw exc_info remains for programmatic access; the
                # handler's formatted output uses ``record.msg``.
                record.msg = record.msg + "\n" + scrub_text(tb_text, self._secrets())
                record.exc_info = None  # avoid double-printing the raw traceback
            except Exception:  # pragma: no cover — defensive
                pass
        return True


def install_redaction(logger_name: str = "vcc", secrets: Iterable[str] | None = None) -> None:
    """Install a :class:`RedactingFilter` on all handlers of ``logger_name``.

    Idempotent: existing RedactingFilter instances are not duplicated.
    """

    logger = logging.getLogger(logger_name)
    filt = RedactingFilter(secrets=secrets)
    for handler in logger.handlers:
        if not any(isinstance(f, RedactingFilter) for f in handler.filters):
            handler.addFilter(filt)
    if not logger.handlers:
        handler = logging.StreamHandler()
        handler.addFilter(filt)
        logger.addHandler(handler)


__all__ = [
    "RedactingFilter",
    "clear_secrets",
    "install_redaction",
    "register_secrets",
    "scrub_exception",
    "scrub_text",
]
