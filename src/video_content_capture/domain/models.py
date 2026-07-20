"""Provider-neutral domain models for the Video Content Capture pipeline.

These Pydantic v2 models are the canonical, provider-neutral data contracts
shared by media, transcription, reporting, storage, and CLI layers. No
provider SDK types are imported here.

Invariants enforced:

* Timing is ordered and nonnegative (words and segments).
* Transcript segments carry stable deterministic string IDs that are unique
  within a transcript and ordered by start time.
* Speakers are anonymous deterministic Traditional Chinese labels such as
  ``講者 A``; personal identities are never inferred.
* Canonical transcript data retains source metadata, raw text, normalized
  text, words, timing, IDs, and speakers.
* Canonical report data expresses required sections, plain-language content,
  and source segment IDs; invalid evidence shapes (unknown IDs or
  source-dependent sections without evidence) are rejected at validation.
"""

from __future__ import annotations

import enum
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

# --- Media metadata ------------------------------------------------------


class StreamInfo(BaseModel):
    """A single probed media stream (container/video/audio/subtitle)."""

    model_config = ConfigDict(extra="forbid")

    index: int
    codec: str
    stream_type: Literal["video", "audio", "subtitle", "container", "data"]
    language: str | None = None
    duration_seconds: float | None = None
    channels: int | None = None
    sample_rate: int | None = None
    width: int | None = None
    height: int | None = None

    @field_validator("duration_seconds")
    @classmethod
    def _duration_nonnegative(cls, value: float | None) -> float | None:
        if value is not None and value < 0:
            raise ValueError("duration_seconds must be nonnegative")
        return value


class MediaMetadata(BaseModel):
    """Probed metadata for a source media file.

    Provider-neutral: the media layer fills this from ``ffprobe`` output.
    """

    model_config = ConfigDict(extra="forbid")

    source_path: str
    container: str
    duration_seconds: float
    video_streams: list[StreamInfo] = Field(default_factory=list)
    audio_streams: list[StreamInfo] = Field(default_factory=list)
    subtitle_streams: list[StreamInfo] = Field(default_factory=list)

    @field_validator("duration_seconds")
    @classmethod
    def _duration_nonnegative(cls, value: float) -> float:
        if value < 0:
            raise ValueError("duration_seconds must be nonnegative")
        return value


# --- Words ---------------------------------------------------------------


class Word(BaseModel):
    """A single recognized word with timing and optional confidence."""

    model_config = ConfigDict(extra="forbid")

    text: str
    start: float
    end: float
    confidence: float | None = None

    @model_validator(mode="after")
    def _validate_timing(self) -> Word:
        if self.start < 0:
            raise ValueError("start must be nonnegative")
        if self.end < self.start:
            raise ValueError("end must not precede start")
        return self


# --- Transcript segments -------------------------------------------------


class TranscriptSegment(BaseModel):
    """A provider-neutral transcript segment.

    Carries a stable deterministic ID, ordered nonnegative timing, raw
    recognized text, normalized Traditional Chinese text, an anonymous
    speaker label, and optional word-level timing.
    """

    model_config = ConfigDict(extra="forbid")

    segment_id: str
    start: float
    end: float
    raw_text: str
    normalized_text: str
    speaker_label: str
    words: list[Word] = Field(default_factory=list)
    confidence: float | None = None

    @field_validator("segment_id")
    @classmethod
    def _segment_id_nonempty(cls, value: str) -> str:
        if not value:
            raise ValueError("segment_id must be a nonempty string")
        return value

    @model_validator(mode="after")
    def _validate_timing(self) -> TranscriptSegment:
        if self.start < 0:
            raise ValueError("start must be nonnegative")
        if self.end < self.start:
            raise ValueError("end must not precede start")
        return self


# --- Transcript ----------------------------------------------------------


class Transcript(BaseModel):
    """A complete canonical transcript.

    Segments are kept ordered by start time with unique stable IDs. Source
    metadata, raw text, normalized text, words, timing, IDs, and speakers are
    all retained for audit and reuse.
    """

    model_config = ConfigDict(extra="forbid")

    metadata: MediaMetadata
    segments: list[TranscriptSegment]
    language: str = "zh-TW"

    @model_validator(mode="after")
    def _validate_segments(self) -> Transcript:
        ids = [seg.segment_id for seg in self.segments]
        if len(set(ids)) != len(ids):
            raise ValueError("transcript segment IDs must be unique")
        # Segments must be ordered by start time. Equal starts are allowed
        # (e.g. overlapping diarization) but a later segment must not start
        # strictly before an earlier one.
        for prev, current in zip(self.segments, self.segments[1:], strict=False):
            if current.start < prev.start:
                raise ValueError("transcript segments must be ordered by start time")
        return self

    @property
    def segment_ids(self) -> set[str]:
        """Return the set of valid source segment IDs for grounding checks."""

        return {seg.segment_id for seg in self.segments}


# --- Report --------------------------------------------------------------


# Mandatory report section types in the canonical Traditional Chinese order.
REQUIRED_REPORT_SECTION_TYPES: tuple[str, ...] = (
    "overview",
    "core_topics",
    "important_numbers",
    "glossary",
    "conclusion",
    "source_index",
)


class ReportItem(BaseModel):
    """A structured, provider-neutral per-item entry in a report section.

    Carries verbatim general-reader ``text`` (NO Markdown bullets, no embedded
    ``（講者觀點）`` label, no presentation prefixes — those are render-time
    concerns owned by the group-9 renderer), an optional ``label`` (used by
    glossary entries to carry the term), the source segment IDs that ground
    the item, and an ``is_speaker_opinion`` flag set by grounding for
    forecasts/judgments/recommendations.

    The renderer (group 9) projects ``text``/``label`` verbatim, appends
    inline evidence links derived from ``source_segment_ids``, and appends a
    ``講者觀點`` label when ``is_speaker_opinion`` is True. It never infers
    opinion from text.
    """

    model_config = ConfigDict(extra="forbid")

    text: str
    label: str | None = None
    source_segment_ids: list[str] = Field(default_factory=list)
    is_speaker_opinion: bool = False


class ReportSection(BaseModel):
    """One section of a grounded plain-language report.

    ``source_segment_ids`` must reference canonical transcript segment IDs.
    A source-dependent section (``is_source_dependent=True``) must carry at
    least one evidence ID; non-source-dependent sections (overview,
    glossary, source index) may have empty evidence.

    ``items`` is the structured per-item representation (group 9 I1/I2): each
    item carries its own verbatim text, evidence IDs, and opinion flag. The
    section-level ``source_segment_ids`` is the deterministic, deduplicated,
    order-preserving union of item evidence IDs (kept for backwards
    compatibility and as the section-level evidence contract). ``content``
    is retained as a free-form fallback for sections that do not decompose
    into items (e.g. a future non-itemized section); itemized renderers
    render from ``items`` when present.
    """

    model_config = ConfigDict(extra="forbid")

    section_type: str
    title: str
    content: str = ""
    items: list[ReportItem] = Field(default_factory=list)
    source_segment_ids: list[str] = Field(default_factory=list)
    is_source_dependent: bool = False

    @model_validator(mode="after")
    def _validate_evidence_shape(self) -> ReportSection:
        if self.is_source_dependent and not self.source_segment_ids:
            raise ValueError(
                f"source-dependent section {self.section_type!r} must reference "
                "at least one source segment ID"
            )
        return self


class Report(BaseModel):
    """A complete canonical grounded report.

    Every section's ``source_segment_ids`` must reference IDs that exist in
    the linked transcript. All required section types must be present exactly
    once.
    """

    model_config = ConfigDict(extra="forbid")

    transcript: Transcript
    sections: list[ReportSection]

    @model_validator(mode="after")
    def _validate_grounding_and_sections(self) -> Report:
        valid_ids = self.transcript.segment_ids
        for section in self.sections:
            for sid in section.source_segment_ids:
                if sid not in valid_ids:
                    raise ValueError(
                        f"section {section.section_type!r} references unknown "
                        f"source segment ID {sid!r}"
                    )
            # Per-item evidence: IDs must be known to the transcript, and a
            # source-dependent section's items must each carry at least one
            # evidence ID (per-item grounding contract).
            for item in section.items:
                if section.is_source_dependent and not item.source_segment_ids:
                    raise ValueError(
                        f"source-dependent section {section.section_type!r} has "
                        "an item with empty source_segment_ids"
                    )
                for sid in item.source_segment_ids:
                    if sid not in valid_ids:
                        raise ValueError(
                            f"section {section.section_type!r} item references "
                            f"unknown source segment ID {sid!r}"
                        )
        present = [section.section_type for section in self.sections]
        missing = [t for t in REQUIRED_REPORT_SECTION_TYPES if t not in present]
        if missing:
            raise ValueError(f"report is missing required section types: {missing}")
        # Each required section type must appear exactly once.
        for required in REQUIRED_REPORT_SECTION_TYPES:
            if present.count(required) > 1:
                raise ValueError(f"report section type {required!r} appears more than once")
        return self


# --- Run metadata and processing-step state -----------------------------


class RunMetadata(BaseModel):
    """Metadata describing one processing attempt.

    Used by the storage layer for resumable, content-and-configuration-keyed
    state. ``content_hash`` is a streamed SHA-256 of the source;
    ``config_hash`` is derived from the effective configuration.
    """

    model_config = ConfigDict(extra="forbid")

    source_path: str
    content_hash: str
    config_hash: str
    attempt_id: str
    started_at: str | None = None
    completed_at: str | None = None


class StepStatus(enum.Enum):
    """Status of a single processing step in an attempt."""

    PENDING = "pending"
    IN_PROGRESS = "in_progress"
    COMPLETED = "completed"
    FAILED = "failed"


class ProcessingStepState(BaseModel):
    """State of one resumable processing step.

    Suitable for later atomic manifest persistence and resume decisions.
    """

    model_config = ConfigDict(extra="forbid")

    step_name: str
    status: StepStatus = StepStatus.PENDING
    started_at: str | None = None
    completed_at: str | None = None
    artifact_path: str | None = None
    error_message: str | None = None

    def mark_completed(self, completed_at: str | None = None) -> None:
        self.status = StepStatus.COMPLETED
        self.completed_at = completed_at
        self.error_message = None

    def mark_failed(self, message: str) -> None:
        self.status = StepStatus.FAILED
        self.error_message = message

    def mark_in_progress(self, started_at: str | None = None) -> None:
        self.status = StepStatus.IN_PROGRESS
        self.started_at = started_at


__all__ = [
    "MediaMetadata",
    "ProcessingStepState",
    "REQUIRED_REPORT_SECTION_TYPES",
    "Report",
    "ReportItem",
    "ReportSection",
    "RunMetadata",
    "StepStatus",
    "StreamInfo",
    "Transcript",
    "TranscriptSegment",
    "Word",
]
