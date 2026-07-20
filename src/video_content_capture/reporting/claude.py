"""Claude (Anthropic) adapter for grounded Traditional Chinese report generation.

This module is the ONLY place the Anthropic Python SDK is imported. It
translates the provider-neutral reporting contracts into SDK calls and maps
provider responses back to the provider-neutral :class:`ReporterResult` /
:class:`Report`. No provider SDK types leak into base/prompts/grounding/
domain/pipeline modules.

Verified against the installed ``anthropic`` SDK (0.117.0) on 2026-07-20.
External public documentation URLs were not reachable from the implementation
environment, so the verification basis is the installed package source,
package metadata, and introspection under Python 3.13.

Verified API surface used here:

* ``anthropic.Anthropic(api_key=..., max_retries=0, timeout=...)`` — the
  client. ``max_retries=0`` disables SDK automatic retries so the adapter's
  bounded application-level retries are not multiplied.
* ``client.messages.parse(model=..., messages=..., system=..., max_tokens=...,
  thinking={"type": "adaptive"}, output_config={"effort": "high"},
  output_format=StructuredReport)`` — structured-output entry point. The
  returned ``ParsedMessage`` exposes ``.parsed_output`` (a parsed
  ``StructuredReport`` or ``None``), ``.id``, ``.model``, and ``.usage``.
* ``output_format`` accepts a Pydantic model class; the SDK derives the
  JSON schema. ``output_config={"effort": "high"}`` requests high-effort
  structured output. ``thinking={"type": "adaptive"}`` enables adaptive
  thinking (Opus 4.8 accepts adaptive thinking only; sampling params are
  removed — no ``temperature``/``top_p``/``top_k`` passed).
* ``claude-opus-4-8`` is a member of the SDK's ``ModelParam`` literal, so the
  SDK accepts it directly as ``model``.
* Exception hierarchy: ``APITimeoutError`` subclasses ``APIConnectionError``;
  ``RateLimitError`` (429), ``InternalServerError`` (5xx), and
  ``OverloadedError`` (529) subclass ``APIStatusError``.

Retry policy: only ``anthropic.APITimeoutError``, ``APIConnectionError``,
``RateLimitError`` (HTTP 429), ``InternalServerError`` / other 5xx, and
``OverloadedError`` (529) are retried with bounded exponential backoff
(``base_delay * 2**attempt``, capped). ``AuthenticationError`` (401),
``PermissionDeniedError`` (403), ``NotFoundError`` (404), ``BadRequestError``
(400), ``UnprocessableEntityError`` (422), a successful response whose
``parsed_output`` is ``None`` or fails schema validation (malformed structured
output), and grounding errors raise the accepted typed domain error
immediately. No secrets in messages, details, raw metadata, or logs.

Map/reduce: long transcripts are split into bounded chronological groups
without reordering (``plan_groups``). Each group is mapped via one
``messages.parse`` call whose structured output references that group's
segment IDs. When more than one map result exists, a reduce call merges the
map results into a single structured report, preserving at least one valid
evidence ID for every retained source-dependent point. Group size/count and
output tokens are bounded by constructor constants.

No Markdown is generated here. The adapter returns a provider-neutral
:class:`ReporterResult` containing a validated :class:`Report` and raw
provider metadata (no secrets). Group 9 owns rendering.
"""

from __future__ import annotations

import json as _json
import random
from typing import Any

# --- Provider SDK import confined to this module ---------------------------
# The SDK ships typed models; the project mypy config (pyproject.toml
# [[tool.mypy.overrides]]) treats anthropic as typed. Importing here keeps
# provider types out of base/prompts/grounding/domain.
import anthropic

from video_content_capture.config import Settings
from video_content_capture.domain.errors import (
    GroundingError,  # noqa: F401 — re-exported for callers; not raised here
    ProviderAuthError,
    ProviderPayloadError,
    RateLimitError,
)
from video_content_capture.domain.models import Transcript
from video_content_capture.reporting.base import (
    Reporter,  # noqa: F401 — re-exported for type hints
    ReporterResult,
    Sleeper,
    StructuredReport,
)
from video_content_capture.reporting.grounding import ground_structured_report
from video_content_capture.reporting.prompts import (
    build_reporting_prompt,
    build_segment_payload,
)

# --- Verified provider constants ------------------------------------------

# Non-5xx HTTP status codes that are retryable per the project spec. Every
# status in the 500-599 range (including 529 overloaded) is handled by
# ``_is_retryable_status`` below.
_RETRYABLE_STATUS_CODES = frozenset({408, 429})

# Authentication/permission/not-found/bad-request/unprocessable are NOT
# retried; they map to the accepted typed domain errors below.
_AUTH_STATUS_CODES = frozenset({401, 403})
_NOT_FOUND_STATUS_CODES = frozenset({404})
_BAD_REQUEST_STATUS_CODES = frozenset({400, 422})

# --- Adapter tunables (bounded group size/count and output tokens) --------

#: Default maximum segments per chronological map group. Bound so each
#: map call stays within a safe reporting context.
DEFAULT_MAX_SEGMENTS_PER_GROUP = 30

#: Default maximum number of map groups. Bound so map/reduce never explodes.
DEFAULT_MAX_GROUPS = 50

#: Default max output tokens for each structured-output call.
DEFAULT_MAX_OUTPUT_TOKENS = 8192

#: Cap on exponential backoff exponent so delays stay bounded.
_BACKOFF_EXP_CAP = 5

#: A reduce call is identified by a single-sentinel "segment" payload; the
#: adapter uses this to ask the reduce call to merge the map results.
_REDUCE_SENTINEL_SEGMENT_ID = "__reduce__"


# --- Adapter --------------------------------------------------------------


class ClaudeReporter:
    """Adapter that generates grounded reports via Claude (Anthropic).

    Provider-neutral: returns :class:`ReporterResult`. All SDK calls are
    confined to this class. Tests patch ``_sdk_parse`` so no paid call is
    made.
    """

    def __init__(
        self,
        *,
        settings: Settings,
        sleeper: Sleeper | None = None,
        max_segments_per_group: int = DEFAULT_MAX_SEGMENTS_PER_GROUP,
        max_groups: int = DEFAULT_MAX_GROUPS,
        max_output_tokens: int = DEFAULT_MAX_OUTPUT_TOKENS,
    ) -> None:
        # Build the SDK client eagerly so credential presence is checked at
        # construction (matches the spec: cloud commands fail before any
        # paid call). ``max_retries=0`` disables SDK automatic retries so the
        # adapter's bounded application-level retries are not multiplied.
        api_key = settings.anthropic_api_key
        if api_key is None:
            # The pipeline layer should validate credentials before calling
            # the adapter; defensively raise a typed error if it did not.
            raise ProviderAuthError("ANTHROPIC_API_KEY is not configured")
        self._client = anthropic.Anthropic(
            api_key=api_key.get_secret_value(),
            max_retries=0,
        )
        self._settings_model = settings
        self._sleeper: Sleeper = sleeper if sleeper is not None else _real_sleep
        if max_segments_per_group < 1:
            raise ValueError("max_segments_per_group must be at least 1")
        if max_groups < 1:
            raise ValueError("max_groups must be at least 1")
        self._max_segments_per_group = max_segments_per_group
        self._max_groups = max_groups
        self._max_output_tokens = max_output_tokens

    # --- Public protocol method -------------------------------------------

    def report(
        self,
        *,
        transcript: Transcript,
        settings: Settings,
    ) -> ReporterResult:
        """Generate a grounded report from a canonical transcript.

        Single-group transcripts go straight through one map call. Long
        transcripts are split into bounded chronological groups, mapped per
        group, then reduced into a single structured report. Grounding
        validation runs on the final structured report before returning.
        """

        groups = self.plan_groups(transcript)
        if len(groups) == 1:
            structured, metadata = self._map_group(transcript, groups[0], settings)
        else:
            map_results: list[StructuredReport] = []
            map_metadatas: list[dict[str, Any]] = []
            for group in groups:
                structured, meta = self._map_group(transcript, group, settings)
                map_results.append(structured)
                map_metadatas.append(meta)
            structured, metadata = self._reduce(transcript, map_results, settings)
            # Preserve all map + reduce message ids for audit.
            metadata = {
                "reduce": metadata,
                "map": map_metadatas,
            }

        # Grounding validation: rejects unknown IDs, empty evidence, malformed
        # sections, out-of-range timing, missing opinion labeling.
        report = ground_structured_report(transcript, structured)
        return ReporterResult(report=report, raw_metadata=_scrub_metadata(metadata))

    # --- Group planning ---------------------------------------------------

    def plan_groups(self, transcript: Transcript) -> list[list[Any]]:
        """Split the transcript into bounded chronological groups.

        Groups preserve canonical (start-time) order and never reorder
        segments. The number of groups is bounded by ``max_groups``; each
        group contains at most ``max_segments_per_group`` segments. When the
        transcript fits in one group, a single one-element list is returned.
        An empty transcript yields a single empty group so the map step still
        receives a deterministic (empty) payload.
        """

        segments = list(transcript.segments)
        if not segments:
            return [[]]
        groups: list[list[Any]] = []
        for i in range(0, len(segments), self._max_segments_per_group):
            if len(groups) >= self._max_groups:
                raise ProviderPayloadError(
                    f"transcript exceeds the configured maximum number of groups "
                    f"({self._max_groups})",
                    details={"groups": len(groups), "max_groups": self._max_groups},
                )
            groups.append(segments[i : i + self._max_segments_per_group])
        return groups

    # --- Map: one structured-output call per group ------------------------

    def _map_group(
        self,
        transcript: Transcript,
        group: list[Any],
        settings: Settings,
    ) -> tuple[StructuredReport, dict[str, Any]]:
        """Map one chronological group to a structured report via messages.parse."""

        system, user = build_reporting_prompt(transcript, segments=group)
        segment_payload = build_segment_payload(transcript, segments=group)
        parsed, meta = self._parse_with_retry(
            settings=settings,
            model=settings.anthropic_model,
            system=system,
            user=user,
            segments=segment_payload,
        )
        structured = _coerce_parsed(parsed)
        return structured, meta

    # --- Reduce: merge map results into one structured report --------------

    def _reduce(
        self,
        transcript: Transcript,
        map_results: list[StructuredReport],
        settings: Settings,
    ) -> tuple[StructuredReport, dict[str, Any]]:
        """Reduce multiple map results into a single structured report.

        The reduce call receives a single sentinel "segment" describing the
        merge task and the JSON of the map results as context, and returns a
        merged :class:`StructuredReport`. The merged result MUST preserve at
        least one valid evidence ID for every retained source-dependent point;
        grounding rejects any retained point that loses all evidence.
        """

        merge_context = _json.dumps(
            [_json.loads(res.model_dump_json()) for res in map_results],
            ensure_ascii=False,
        )
        sentinel_segment = [
            {
                "segment_id": _REDUCE_SENTINEL_SEGMENT_ID,
                "start_seconds": 0.0,
                "end_seconds": 0.0,
                "speaker_label": "講者 A",
                "normalized_text": (
                    "請合併並去重以下 map 階段結果，保留每個依賴來源項目的至少一個有效 segment ID。"
                    f" map 結果（JSON）：{merge_context}"
                ),
            }
        ]
        system, user = build_reporting_prompt(transcript, segments=sentinel_segment)
        parsed, meta = self._parse_with_retry(
            settings=settings,
            model=settings.anthropic_model,
            system=system,
            user=user,
            segments=sentinel_segment,
        )
        structured = _coerce_parsed(parsed)
        return structured, meta

    # --- Retry wrapper ----------------------------------------------------

    def _parse_with_retry(
        self,
        *,
        settings: Settings,
        model: str,
        system: str,
        user: str,
        segments: list[Any],
    ) -> tuple[Any, dict[str, Any]]:
        """Call ``messages.parse`` with bounded exponential backoff.

        Retries ONLY timeouts, connection errors, RateLimitError (429), and
        server/overloaded 5xx. Auth/permission/not-found/bad-request/
        unprocessable and malformed structured output raise typed errors
        immediately.
        """

        attempt = 0
        last_exc: BaseException | None = None
        max_retries = settings.max_retries
        while attempt < max_retries:
            attempt += 1
            try:
                parsed = self._sdk_parse(
                    settings=settings,
                    model=settings.anthropic_model,
                    system=system,
                    user=user,
                    segments=segments,
                )
                return parsed, _metadata_from_parsed(parsed)
            except anthropic.APIStatusError as exc:
                # APITimeoutError subclasses APIConnectionError; both are
                # NOT APIStatusError, so they are handled in the next branch.
                status = getattr(exc, "status_code", None)
                if status in _AUTH_STATUS_CODES:
                    raise ProviderAuthError(
                        "Anthropic authentication or permission failed",
                        details={"status": status},
                    ) from exc
                if status in _NOT_FOUND_STATUS_CODES:
                    raise ProviderPayloadError(
                        "Anthropic resource not found",
                        details={"status": status},
                    ) from exc
                if status in _BAD_REQUEST_STATUS_CODES:
                    raise ProviderPayloadError(
                        "Anthropic rejected the request (bad request or unprocessable)",
                        details={"status": status},
                    ) from exc
                if _is_retryable_status(status):
                    last_exc = exc
                    self._sleep_before_next(attempt, max_retries, settings, reason="server")
                    continue
                # Unknown status: treat as non-retryable payload error.
                raise ProviderPayloadError(
                    f"Anthropic returned an unexpected status {status}",
                    details={"status": status},
                ) from exc
            except anthropic.APITimeoutError as exc:
                last_exc = exc
                self._sleep_before_next(attempt, max_retries, settings, reason="timeout")
                continue
            except anthropic.APIConnectionError as exc:
                last_exc = exc
                self._sleep_before_next(attempt, max_retries, settings, reason="connection")
                continue

        # Exhausted retries on a transient failure.
        raise RateLimitError(
            "Anthropic transient failure after retries",
            details={"attempts": attempt},
        ) from last_exc

    def _sleep_before_next(
        self,
        attempt: int,
        max_retries: int,
        settings: Settings,
        *,
        reason: str,
    ) -> None:
        """Sleep before the next attempt, or raise RateLimitError when exhausted."""

        if attempt >= max_retries:
            raise RateLimitError(
                f"Anthropic {reason} failure after retries",
                details={"attempts": attempt, "reason": reason},
            )
        delay = _backoff_delay(base=settings.retry_base_delay_seconds, attempt=attempt - 1)
        self._sleeper(delay)

    # --- SDK call indirection (mockable) -----------------------------------

    def _sdk_parse(
        self,
        *,
        settings: Settings,
        model: str,
        system: str,
        user: str,
        segments: list[Any],
    ) -> Any:
        """Submit one structured-output call via the SDK.

        ``model`` is passed explicitly (rather than read from ``settings``
        inside) so tests can capture the configured model at the SDK client
        boundary. Tests patch this method to return a fake
        ``ParsedMessage``-like object (exposing ``parsed_output``, ``id``,
        ``model``, ``usage``) and avoid any paid call.
        """

        # ``MessageParam`` is a TypedDict; the SDK accepts a list of dict-like
        # messages. We cast to ``Any`` to avoid tight coupling to the SDK's
        # internal TypedDict shape while keeping the call site readable.
        messages: Any = [{"role": "user", "content": user}]
        return self._client.messages.parse(
            model=model,
            messages=messages,
            system=system,
            max_tokens=self._max_output_tokens,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            output_format=StructuredReport,
        )


# --- Helpers ---------------------------------------------------------------


def _coerce_parsed(parsed: Any) -> StructuredReport:
    """Coerce a parsed message's ``parsed_output`` into a StructuredReport.

    A successful response whose ``parsed_output`` is ``None`` or fails schema
    validation is a malformed structured output and raises
    :class:`ProviderPayloadError` (NOT retried).
    """

    payload = getattr(parsed, "parsed_output", None)
    if payload is None:
        raise ProviderPayloadError(
            "Anthropic returned a response with no parsed structured output",
            details={},
        )
    if isinstance(payload, StructuredReport):
        return payload
    if isinstance(payload, dict):
        try:
            return StructuredReport.model_validate(payload)
        except Exception as exc:  # noqa: BLE001 — provider payload, surface as typed
            raise ProviderPayloadError(
                "Anthropic structured output failed schema validation",
                details={"error": str(exc)},
            ) from exc
    # Pydantic model of a different type: attempt conversion via JSON.
    try:
        return StructuredReport.model_validate(payload)
    except Exception as exc:  # noqa: BLE001 — provider payload, surface as typed
        raise ProviderPayloadError(
            "Anthropic structured output failed schema validation",
            details={"error": str(exc)},
        ) from exc


def _metadata_from_parsed(parsed: Any) -> dict[str, Any]:
    """Extract secret-free raw provider metadata from a parsed message."""

    usage = getattr(parsed, "usage", None)
    usage_dict: dict[str, Any] = {}
    if usage is not None:
        try:
            usage_dict = usage.model_dump()
        except AttributeError:
            usage_dict = dict(usage) if isinstance(usage, dict) else {}
    return {
        "message_id": getattr(parsed, "id", None),
        "model": getattr(parsed, "model", None),
        "usage": usage_dict,
    }


def _scrub_metadata(metadata: dict[str, Any]) -> dict[str, Any]:
    """Return raw provider metadata, confirmed secret-free by construction.

    The metadata only carries message id, model, and usage — no credentials
    are ever placed there. This function exists as a defense-in-depth seam:
    a future change can scan/scrub here without altering call sites.
    """

    return metadata


def _is_retryable_status(status: int | None) -> bool:
    return status in _RETRYABLE_STATUS_CODES or (status is not None and 500 <= status <= 599)


def _backoff_delay(*, base: float, attempt: int) -> float:
    """Bounded exponential backoff delay (``base * 2**attempt``, capped, + jitter)."""

    if base <= 0:
        return 0.0
    exp = min(attempt, _BACKOFF_EXP_CAP)
    delay = base * (2**exp)
    jitter = delay * 0.2 * random.random()  # noqa: S311 — jitter only
    return float(delay + jitter)


def _real_sleep(seconds: float) -> None:
    """Default sleeper that actually sleeps (used in production)."""

    import time

    time.sleep(seconds)


__all__ = ["ClaudeReporter"]
