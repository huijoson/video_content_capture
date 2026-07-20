"""Provider-neutral reporting contracts.

This module defines the contracts a grounded report provider must implement
WITHOUT importing any provider SDK. The domain layer, the pipeline layer, and
group 9's renderer depend on these contracts, never on provider-specific
types.

Contracts:

* :class:`Reporter` — runtime-checkable protocol returning a
  :class:`ReporterResult` for one canonical :class:`Transcript`.
* :class:`ReporterResult` — provider-neutral result: a validated
  :class:`Report` (provider-neutral domain model) plus raw provider response
  metadata for storage/audit (never Markdown, never secrets).
* Evidence-bearing structured response models (:class:`StructuredReport` and
  per-section models) — the deterministic shape the model returns via
  structured outputs. Every source-dependent item carries ``source_segment_ids``.
* :data:`Sleeper` — injectable backoff delay callable so tests never sleep.

The six required Traditional Chinese report sections are:

* ``overview`` — 三分鐘掌握影片
* ``core_topics`` — 核心重點
* ``important_numbers`` — 重要數字與說法
* ``glossary`` — 名詞白話解釋
* ``conclusion`` — 結論與可能影響
* ``source_index`` — 來源索引

These structured models carry NO timestamp field: timestamps are derived
from canonical transcript segments by code (group 9 renderer), never authored
by the model.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any, Protocol, runtime_checkable

from pydantic import BaseModel, ConfigDict, Field, field_validator

from video_content_capture.config import Settings
from video_content_capture.domain.models import Report, Transcript

# --- Required structured section types ------------------------------------

#: The six required structured report section types, in the canonical
#: Traditional Chinese rendering order. Each MUST appear exactly once in a
#: validated :class:`StructuredReport`.
REQUIRED_STRUCTURED_SECTION_TYPES: tuple[str, ...] = (
    "overview",
    "core_topics",
    "important_numbers",
    "glossary",
    "conclusion",
    "source_index",
)

#: Section types whose items MUST carry at least one source segment ID.
SOURCE_DEPENDENT_SECTION_TYPES: frozenset[str] = frozenset(
    {"core_topics", "important_numbers", "conclusion"}
)


# --- Injectable boundaries -------------------------------------------------


#: Type of an injectable sleeper callable used for bounded retry backoff.
#: Tests inject a no-op sleeper so retry tests never block on real time.
Sleeper = Callable[[float], None]


# --- Evidence-bearing structured response models --------------------------
#
# These are the deterministic shape the model returns via the SDK's
# structured-output mechanism (``messages.parse(output_format=...)``). They
# are provider-neutral (no SDK imports) and carry evidence IDs on every
# source-dependent item. They carry NO timestamp fields.


class _EvidenceItem(BaseModel):
    """Base mix-in validating source-dependent evidence."""

    model_config = ConfigDict(extra="forbid")

    source_segment_ids: list[str] = Field(default_factory=list)


class StructuredOverview(_EvidenceItem):
    """三分鐘掌握影片 — short overview for general readers.

    The overview summarizes the program; it is not strictly source-dependent
    (it is a high-level digest), so empty evidence is allowed. When evidence
    is provided, the IDs MUST be valid (grounding checks that).
    """

    summary: str = Field(min_length=1)


class StructuredCoreTopic(_EvidenceItem):
    """核心重點 — one core topic with evidence.

    Source-dependent: the grounding layer requires at least one source
    segment ID (a :class:`StructuredReport` with empty evidence here parses
    successfully so grounding can reject it with a :class:`GroundingError`
    rather than a schema error). ``is_speaker_opinion`` flags whether the
    topic expresses a forecast, judgment, or recommendation (labeled
    講者觀點 by the renderer).
    """

    topic: str = Field(min_length=1)
    is_speaker_opinion: bool = False


class StructuredImportantNumber(_EvidenceItem):
    """重要數字與說法 — one important number or claim with evidence.

    Source-dependent: the grounding layer requires at least one source
    segment ID (empty evidence parses so grounding can reject it with a
    :class:`GroundingError`). ``is_speaker_opinion`` flags a
    forecast/judgment/recommendation.
    """

    number_or_claim: str = Field(min_length=1)
    is_speaker_opinion: bool = False


class StructuredGlossaryEntry(_EvidenceItem):
    """名詞白話解釋 — one financial-term glossary entry.

    A glossary entry explains a term in plain language for general readers.
    It is NOT source-dependent (the explanation is general-knowledge
    phrasing), so empty evidence is allowed. When evidence is provided, the
    IDs MUST be valid.
    """

    term: str = Field(min_length=1)
    explanation: str = Field(min_length=1)


class StructuredConclusion(_EvidenceItem):
    """結論與可能影響 — conclusion and possible impact with evidence.

    Source-dependent: the grounding layer requires at least one source
    segment ID (empty evidence parses so grounding can reject it with a
    :class:`GroundingError`). ``is_speaker_opinion`` flags a
    forecast/judgment/recommendation; the grounding layer requires this flag
    to be ``True`` when the conclusion text reads as a viewpoint (so
    forecasts are labeled 講者觀點).
    """

    conclusion: str = Field(min_length=1)
    possible_impact: str = Field(min_length=1)
    is_speaker_opinion: bool = False


class StructuredSourceIndex(BaseModel):
    """來源索引 — the source index listing referenced segment IDs.

    ``entries`` is the list of canonical segment IDs referenced by the report.
    The renderer (group 9) derives timestamps and transcript-anchor links
    from these IDs in code; the model never supplies timestamps.
    """

    model_config = ConfigDict(extra="forbid")

    entries: list[str] = Field(default_factory=list)

    @field_validator("entries")
    @classmethod
    def _dedupe_preserve_order(cls, value: list[str]) -> list[str]:
        seen: set[str] = set()
        out: list[str] = []
        for sid in value:
            if sid not in seen:
                seen.add(sid)
                out.append(sid)
        return out


class StructuredReport(BaseModel):
    """The full model-facing structured response covering all six sections.

    Every required section type MUST be present exactly once. The model
    returns this shape via structured outputs; the grounding layer validates
    it against the canonical transcript (unknown IDs, empty evidence,
    out-of-range timing, opinion labeling) before producing a provider-neutral
    :class:`Report`.
    """

    model_config = ConfigDict(extra="forbid")

    overview: StructuredOverview
    core_topics: list[StructuredCoreTopic] = Field(min_length=1)
    important_numbers: list[StructuredImportantNumber] = Field(min_length=1)
    glossary: list[StructuredGlossaryEntry] = Field(min_length=1)
    conclusion: StructuredConclusion
    source_index: StructuredSourceIndex


# --- Provider-neutral result ----------------------------------------------


class ReporterResult:
    """Provider-neutral result of generating a grounded report.

    Attributes:
        report: provider-neutral validated :class:`Report` (the canonical
            domain model consumed by group 9's renderer and storage).
        raw_metadata: raw provider response metadata retained for storage
            and audit (e.g. message id, model, token usage). NEVER contains
            Markdown, and NEVER contains credentials (the adapter scrubs
            these before exposing metadata).
    """

    __slots__ = ("report", "raw_metadata")

    def __init__(self, *, report: Report, raw_metadata: dict[str, Any]) -> None:
        self.report = report
        self.raw_metadata = raw_metadata


# --- Reporter protocol ----------------------------------------------------


@runtime_checkable
class Reporter(Protocol):
    """Provider-neutral grounded reporting protocol.

    Implementations generate a Traditional Chinese, evidence-grounded report
    from a canonical :class:`Transcript` and return a
    :class:`ReporterResult`. They MUST:

    * Send the model ordered structured transcript segments (ID, times as
      context, anonymous speaker, normalized text) — never raw timestamps the
      model could present as authoritative.
    * Require the model to return source segment IDs (not timestamps/links/
      Markdown) on every source-dependent item.
    * For long transcripts, split into bounded chronological groups without
      reordering; map outputs retain evidence IDs; reduce merges/deduplicates
      while preserving at least one valid evidence ID for every retained
      source-dependent point.
    * Run grounding validation against the canonical transcript BEFORE
      returning, rejecting unknown IDs, empty evidence for source-dependent
      claims, malformed/missing/duplicate sections, out-of-range referenced
      timing, and missing 講者觀點 viewpoint labeling.
    * Retry only timeouts, connection errors, rate limits, and server/5xx
      failures with bounded exponential backoff; stop immediately on auth,
      permission, not-found, bad-request, unprocessable, malformed structured
      output, and grounding errors.
    * NOT generate Markdown (group 9 owns rendering) and NOT include
      credentials in any message, detail, response model, log, or artifact.
    """

    def report(
        self,
        *,
        transcript: Transcript,
        settings: Settings,
    ) -> ReporterResult:
        """Generate a grounded report from a canonical transcript."""
        ...


__all__ = [
    "REQUIRED_STRUCTURED_SECTION_TYPES",
    "Reporter",
    "ReporterResult",
    "Sleeper",
    "SOURCE_DEPENDENT_SECTION_TYPES",
    "StructuredConclusion",
    "StructuredCoreTopic",
    "StructuredGlossaryEntry",
    "StructuredImportantNumber",
    "StructuredOverview",
    "StructuredReport",
    "StructuredSourceIndex",
]
