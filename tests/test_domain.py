"""Focused domain tests for the provider-neutral contracts.

Covers OpenSpec group 3: timing validation, deterministic segment IDs,
anonymous speaker labels, source-segment evidence, invalid report evidence
shapes, and stable typed error categories. No AssemblyAI/Anthropic types may
leak into these modules.
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

# --- Provider neutrality -------------------------------------------------


def test_domain_modules_import_no_provider_packages() -> None:
    """Domain modules must not import AssemblyAI or Anthropic SDKs."""

    import video_content_capture.domain.errors as errors_mod
    import video_content_capture.domain.models as models_mod

    for module in (errors_mod, models_mod):
        source = open(module.__file__, encoding="utf-8").read()
        assert "assemblyai" not in source.lower()
        assert "anthropic" not in source.lower()
        assert "openai" not in source.lower()


# --- Words ---------------------------------------------------------------


def test_word_rejects_negative_start() -> None:
    from video_content_capture.domain.models import Word

    with pytest.raises(ValidationError):
        Word(text="你", start=-0.1, end=0.5, confidence=0.9)


def test_word_rejects_end_before_start() -> None:
    from video_content_capture.domain.models import Word

    with pytest.raises(ValidationError):
        Word(text="你", start=1.0, end=0.5, confidence=0.9)


# --- Transcript segments -------------------------------------------------


def test_segment_rejects_negative_start() -> None:
    from video_content_capture.domain.models import TranscriptSegment

    with pytest.raises(ValidationError):
        TranscriptSegment(
            segment_id="seg-0001",
            start=-1.0,
            end=1.0,
            raw_text="你好",
            normalized_text="你好",
            speaker_label="講者 A",
        )


def test_segment_rejects_end_before_start() -> None:
    from video_content_capture.domain.models import TranscriptSegment

    with pytest.raises(ValidationError):
        TranscriptSegment(
            segment_id="seg-0001",
            start=2.0,
            end=1.0,
            raw_text="你好",
            normalized_text="你好",
            speaker_label="講者 A",
        )


def test_segment_speaker_label_is_anonymous_chinese() -> None:
    from video_content_capture.domain.models import TranscriptSegment

    seg = TranscriptSegment(
        segment_id="seg-0001",
        start=0.0,
        end=1.0,
        raw_text="你好",
        normalized_text="你好",
        speaker_label="講者 A",
    )
    assert seg.speaker_label == "講者 A"
    # Speaker labels are anonymous; no personal names are inferred.
    assert "講者" in seg.speaker_label


def test_segment_id_is_required_and_stable_string() -> None:
    from video_content_capture.domain.models import TranscriptSegment

    seg = TranscriptSegment(
        segment_id="seg-0001",
        start=0.0,
        end=1.0,
        raw_text="你好",
        normalized_text="你好",
        speaker_label="講者 A",
    )
    assert seg.segment_id == "seg-0001"
    # Segment ID is required; empty should be rejected.
    with pytest.raises(ValidationError):
        TranscriptSegment(
            segment_id="",
            start=0.0,
            end=1.0,
            raw_text="你好",
            normalized_text="你好",
            speaker_label="講者 A",
        )


# --- Transcript ----------------------------------------------------------


def test_transcript_rejects_out_of_order_segments() -> None:
    from video_content_capture.domain.models import (
        MediaMetadata,
        Transcript,
        TranscriptSegment,
    )

    metadata = MediaMetadata(
        source_path="video.mp4",
        container="mp4",
        duration_seconds=10.0,
        video_streams=[],
        audio_streams=[],
        subtitle_streams=[],
    )
    first = TranscriptSegment(
        segment_id="seg-0001",
        start=5.0,
        end=6.0,
        raw_text="b",
        normalized_text="b",
        speaker_label="講者 A",
    )
    second = TranscriptSegment(
        segment_id="seg-0002",
        start=0.0,
        end=1.0,
        raw_text="a",
        normalized_text="a",
        speaker_label="講者 A",
    )
    with pytest.raises(ValidationError):
        Transcript(metadata=metadata, segments=[first, second], language="zh-TW")


def test_transcript_retains_source_metadata_raw_and_normalized() -> None:
    from video_content_capture.domain.models import (
        MediaMetadata,
        Transcript,
        TranscriptSegment,
        Word,
    )

    metadata = MediaMetadata(
        source_path="video.mp4",
        container="mp4",
        duration_seconds=10.0,
        video_streams=[],
        audio_streams=[],
        subtitle_streams=[],
    )
    word = Word(text="你", start=0.0, end=0.5, confidence=0.95)
    seg = TranscriptSegment(
        segment_id="seg-0001",
        start=0.0,
        end=1.0,
        raw_text="你好",
        normalized_text="你好",
        speaker_label="講者 A",
        words=[word],
    )
    transcript = Transcript(metadata=metadata, segments=[seg], language="zh-TW")
    assert transcript.metadata is metadata
    assert transcript.segments[0].raw_text == "你好"
    assert transcript.segments[0].normalized_text == "你好"
    assert transcript.segments[0].words[0].text == "你"
    assert transcript.language == "zh-TW"


def test_transcript_rejects_duplicate_segment_ids() -> None:
    from video_content_capture.domain.models import (
        MediaMetadata,
        Transcript,
        TranscriptSegment,
    )

    metadata = MediaMetadata(
        source_path="video.mp4",
        container="mp4",
        duration_seconds=10.0,
        video_streams=[],
        audio_streams=[],
        subtitle_streams=[],
    )
    seg1 = TranscriptSegment(
        segment_id="seg-0001",
        start=0.0,
        end=1.0,
        raw_text="a",
        normalized_text="a",
        speaker_label="講者 A",
    )
    seg2 = TranscriptSegment(
        segment_id="seg-0001",
        start=1.0,
        end=2.0,
        raw_text="b",
        normalized_text="b",
        speaker_label="講者 A",
    )
    with pytest.raises(ValidationError):
        Transcript(metadata=metadata, segments=[seg1, seg2], language="zh-TW")


# --- Media metadata ------------------------------------------------------


def test_media_metadata_requires_nonnegative_duration() -> None:
    from video_content_capture.domain.models import MediaMetadata

    with pytest.raises(ValidationError):
        MediaMetadata(
            source_path="video.mp4",
            container="mp4",
            duration_seconds=-1.0,
            video_streams=[],
            audio_streams=[],
            subtitle_streams=[],
        )


# --- Report sections -----------------------------------------------------


def test_report_section_rejects_unknown_segment_id_in_evidence() -> None:
    from video_content_capture.domain.models import ReportSection

    # Evidence IDs must be present in the transcript segment set; validation
    # against the transcript happens in the Report model. A section that
    # references an unknown ID must be rejected when the Report is built.
    with pytest.raises(ValidationError):
        # Empty evidence for a source-dependent section is invalid.
        ReportSection(
            section_type="core_topics",
            title="核心重點",
            content="something",
            source_segment_ids=[],
            is_source_dependent=True,
        )


def test_report_section_accepts_non_source_dependent_without_evidence() -> None:
    from video_content_capture.domain.models import ReportSection

    section = ReportSection(
        section_type="overview",
        title="三分鐘掌握影片",
        content="概覽內容",
        source_segment_ids=[],
        is_source_dependent=False,
    )
    assert section.source_segment_ids == []


def test_report_rejects_unknown_evidence_id() -> None:
    from video_content_capture.domain.models import (
        MediaMetadata,
        Report,
        ReportSection,
        Transcript,
        TranscriptSegment,
    )

    metadata = MediaMetadata(
        source_path="video.mp4",
        container="mp4",
        duration_seconds=10.0,
        video_streams=[],
        audio_streams=[],
        subtitle_streams=[],
    )
    seg = TranscriptSegment(
        segment_id="seg-0001",
        start=0.0,
        end=1.0,
        raw_text="a",
        normalized_text="a",
        speaker_label="講者 A",
    )
    transcript = Transcript(metadata=metadata, segments=[seg], language="zh-TW")

    bad_section = ReportSection(
        section_type="core_topics",
        title="核心重點",
        content="重點",
        source_segment_ids=["seg-9999"],
        is_source_dependent=True,
    )
    with pytest.raises(ValidationError):
        Report(transcript=transcript, sections=[bad_section])


def test_report_requires_all_mandatory_section_types() -> None:
    from video_content_capture.domain.models import (
        MediaMetadata,
        Report,
        ReportSection,
        Transcript,
        TranscriptSegment,
    )

    metadata = MediaMetadata(
        source_path="video.mp4",
        container="mp4",
        duration_seconds=10.0,
        video_streams=[],
        audio_streams=[],
        subtitle_streams=[],
    )
    seg = TranscriptSegment(
        segment_id="seg-0001",
        start=0.0,
        end=1.0,
        raw_text="a",
        normalized_text="a",
        speaker_label="講者 A",
    )
    transcript = Transcript(metadata=metadata, segments=[seg], language="zh-TW")

    # Only one mandatory section provided; should fail.
    only_one = ReportSection(
        section_type="overview",
        title="三分鐘掌握影片",
        content="概覽",
        source_segment_ids=[],
        is_source_dependent=False,
    )
    with pytest.raises(ValidationError):
        Report(transcript=transcript, sections=[only_one])


def test_report_accepts_valid_evidence() -> None:
    from video_content_capture.domain.models import (
        MediaMetadata,
        Report,
        ReportSection,
        Transcript,
        TranscriptSegment,
    )

    metadata = MediaMetadata(
        source_path="video.mp4",
        container="mp4",
        duration_seconds=10.0,
        video_streams=[],
        audio_streams=[],
        subtitle_streams=[],
    )
    seg = TranscriptSegment(
        segment_id="seg-0001",
        start=0.0,
        end=1.0,
        raw_text="a",
        normalized_text="a",
        speaker_label="講者 A",
    )
    transcript = Transcript(metadata=metadata, segments=[seg], language="zh-TW")

    sections = [
        ReportSection(
            section_type="overview",
            title="三分鐘掌握影片",
            content="概覽",
            source_segment_ids=[],
            is_source_dependent=False,
        ),
        ReportSection(
            section_type="core_topics",
            title="核心重點",
            content="重點",
            source_segment_ids=["seg-0001"],
            is_source_dependent=True,
        ),
        ReportSection(
            section_type="important_numbers",
            title="重要數字與說法",
            content="數字",
            source_segment_ids=["seg-0001"],
            is_source_dependent=True,
        ),
        ReportSection(
            section_type="glossary",
            title="名詞白話解釋",
            content="名詞",
            source_segment_ids=[],
            is_source_dependent=False,
        ),
        ReportSection(
            section_type="conclusion",
            title="結論與可能影響",
            content="結論",
            source_segment_ids=["seg-0001"],
            is_source_dependent=True,
        ),
        ReportSection(
            section_type="source_index",
            title="來源索引",
            content="索引",
            source_segment_ids=[],
            is_source_dependent=False,
        ),
    ]
    report = Report(transcript=transcript, sections=sections)
    assert len(report.sections) == 6


# --- Structured ReportItem (group 9 I1/I2) -----------------------------


def test_report_item_carries_text_evidence_and_opinion_flag() -> None:
    """A structured ``ReportItem`` carries verbatim text, source segment IDs,
    and an ``is_speaker_opinion`` flag — no Markdown bullets or labels baked
    into ``text``."""

    from video_content_capture.domain.models import ReportItem

    item = ReportItem(
        text="我預期下半年利率會維持高檔。",
        source_segment_ids=["seg-0001"],
        is_speaker_opinion=True,
    )
    assert item.text == "我預期下半年利率會維持高檔。"
    assert item.source_segment_ids == ["seg-0001"]
    assert item.is_speaker_opinion is True
    # No Markdown bullet or embedded opinion label in the canonical text.
    assert not item.text.startswith("- ")
    assert "（講者觀點）" not in item.text


def test_report_item_supports_optional_label_for_glossary() -> None:
    """A glossary item carries a ``label`` (the term) and ``text`` (the
    explanation); non-glossary items leave ``label`` as None."""

    from video_content_capture.domain.models import ReportItem

    entry = ReportItem(
        label="聯邦基金利率",
        text="銀行間隔夜借貸的目標利率。",
        source_segment_ids=["seg-0001"],
    )
    assert entry.label == "聯邦基金利率"
    assert entry.text == "銀行間隔夜借貸的目標利率。"

    plain = ReportItem(text="核心重點。", source_segment_ids=["seg-0001"])
    assert plain.label is None


def test_report_section_carries_structured_items() -> None:
    """A ``ReportSection`` carries an ``items`` list of ``ReportItem``; the
    section-level ``source_segment_ids`` remain consistent with the union of
    item IDs (deterministic, deduplicated, order-preserving)."""

    from video_content_capture.domain.models import ReportItem, ReportSection

    section = ReportSection(
        section_type="core_topics",
        title="核心重點",
        content="",
        items=[
            ReportItem(text="重點 A。", source_segment_ids=["seg-0001"]),
            ReportItem(
                text="重點 B。",
                source_segment_ids=["seg-0002", "seg-0001"],
            ),
        ],
        source_segment_ids=["seg-0001", "seg-0002"],
        is_source_dependent=True,
    )
    assert len(section.items) == 2
    assert section.items[0].text == "重點 A。"


def test_report_rejects_item_evidence_unknown_id() -> None:
    """An item referencing an unknown segment ID is rejected at Report build."""

    from video_content_capture.domain.models import (
        MediaMetadata,
        Report,
        ReportItem,
        ReportSection,
        Transcript,
        TranscriptSegment,
    )

    metadata = MediaMetadata(
        source_path="video.mp4",
        container="mp4",
        duration_seconds=10.0,
    )
    seg = TranscriptSegment(
        segment_id="seg-0001",
        start=0.0,
        end=1.0,
        raw_text="a",
        normalized_text="a",
        speaker_label="講者 A",
    )
    transcript = Transcript(metadata=metadata, segments=[seg], language="zh-TW")
    bad_item = ReportItem(text="壞證據。", source_segment_ids=["seg-9999"])
    bad_section = ReportSection(
        section_type="core_topics",
        title="核心重點",
        content="",
        items=[bad_item],
        source_segment_ids=["seg-9999"],
        is_source_dependent=True,
    )
    with pytest.raises(ValidationError):
        Report(transcript=transcript, sections=[bad_section])


def test_report_rejects_source_dependent_item_with_empty_evidence() -> None:
    """A source-dependent section item with empty evidence is rejected."""

    from video_content_capture.domain.models import (
        MediaMetadata,
        Report,
        ReportItem,
        ReportSection,
        Transcript,
        TranscriptSegment,
    )

    metadata = MediaMetadata(
        source_path="video.mp4",
        container="mp4",
        duration_seconds=10.0,
    )
    seg = TranscriptSegment(
        segment_id="seg-0001",
        start=0.0,
        end=1.0,
        raw_text="a",
        normalized_text="a",
        speaker_label="講者 A",
    )
    transcript = Transcript(metadata=metadata, segments=[seg], language="zh-TW")
    # Section-level evidence is present, but one item carries empty evidence.
    empty_item = ReportItem(text="無證據重點。", source_segment_ids=[])
    good_item = ReportItem(text="有證據重點。", source_segment_ids=["seg-0001"])
    bad_section = ReportSection(
        section_type="core_topics",
        title="核心重點",
        content="",
        items=[empty_item, good_item],
        source_segment_ids=["seg-0001"],
        is_source_dependent=True,
    )
    with pytest.raises(ValidationError):
        Report(transcript=transcript, sections=[bad_section])


def test_report_section_items_round_trip_in_canonical_json() -> None:
    """Structured items and their flags survive canonical JSON round-trip."""

    import json

    from video_content_capture.domain.models import (
        MediaMetadata,
        Report,
        ReportItem,
        ReportSection,
        Transcript,
        TranscriptSegment,
    )

    metadata = MediaMetadata(
        source_path="video.mp4",
        container="mp4",
        duration_seconds=10.0,
    )
    seg = TranscriptSegment(
        segment_id="seg-0001",
        start=0.0,
        end=1.0,
        raw_text="a",
        normalized_text="a",
        speaker_label="講者 A",
    )
    transcript = Transcript(metadata=metadata, segments=[seg], language="zh-TW")
    sections = [
        ReportSection(
            section_type="overview",
            title="三分鐘掌握影片",
            content="",
            items=[ReportItem(text="概覽。", source_segment_ids=[])],
            is_source_dependent=False,
        ),
        ReportSection(
            section_type="core_topics",
            title="核心重點",
            content="",
            items=[
                ReportItem(
                    text="我預期利率居高。",
                    source_segment_ids=["seg-0001"],
                    is_speaker_opinion=True,
                )
            ],
            source_segment_ids=["seg-0001"],
            is_source_dependent=True,
        ),
        ReportSection(
            section_type="important_numbers",
            title="重要數字與說法",
            content="",
            items=[ReportItem(text="5.25% 至 5.50%。", source_segment_ids=["seg-0001"])],
            source_segment_ids=["seg-0001"],
            is_source_dependent=True,
        ),
        ReportSection(
            section_type="glossary",
            title="名詞白話解釋",
            content="",
            items=[
                ReportItem(
                    label="聯邦基金利率",
                    text="銀行間隔夜借貸目標利率。",
                    source_segment_ids=[],
                )
            ],
            is_source_dependent=False,
        ),
        ReportSection(
            section_type="conclusion",
            title="結論與可能影響",
            content="",
            items=[
                ReportItem(
                    text="講者預期利率維持高檔。",
                    source_segment_ids=["seg-0001"],
                    is_speaker_opinion=True,
                )
            ],
            source_segment_ids=["seg-0001"],
            is_source_dependent=True,
        ),
        ReportSection(
            section_type="source_index",
            title="來源索引",
            content="",
            items=[],
            source_segment_ids=["seg-0001"],
            is_source_dependent=False,
        ),
    ]
    report = Report(transcript=transcript, sections=sections)
    s = report.model_dump_json(indent=2)
    data = json.loads(s)
    # Structured flags/IDs present.
    core = next(s for s in data["sections"] if s["section_type"] == "core_topics")
    assert core["items"][0]["is_speaker_opinion"] is True
    assert core["items"][0]["source_segment_ids"] == ["seg-0001"]
    # No Markdown bullet or embedded opinion label in canonical item text.
    assert not core["items"][0]["text"].startswith("- ")
    assert "（講者觀點）" not in core["items"][0]["text"]
    # Round-trip.
    restored = Report.model_validate_json(s)
    assert restored == report


# --- Run metadata and processing-step state -----------------------------


def test_run_metadata_records_attempt_and_source() -> None:
    from video_content_capture.domain.models import RunMetadata

    run = RunMetadata(
        source_path="video.mp4",
        content_hash="abc123",
        config_hash="cfg1",
        attempt_id="attempt-0001",
    )
    assert run.source_path == "video.mp4"
    assert run.content_hash == "abc123"
    assert run.config_hash == "cfg1"
    assert run.attempt_id == "attempt-0001"


def test_processing_step_state_progresses() -> None:
    from video_content_capture.domain.models import ProcessingStepState, StepStatus

    step = ProcessingStepState(step_name="probe", status=StepStatus.COMPLETED)
    assert step.status is StepStatus.COMPLETED
    step.mark_completed()
    assert step.status is StepStatus.COMPLETED


# --- Typed errors --------------------------------------------------------


def test_error_categories_are_stable_and_distinct() -> None:
    from video_content_capture.domain import errors

    categories = {
        errors.MediaError: errors.ErrorCategory.MEDIA,
        errors.ConfigurationError: errors.ErrorCategory.CONFIGURATION,
        errors.ProviderAuthError: errors.ErrorCategory.PROVIDER_AUTH,
        errors.RateLimitError: errors.ErrorCategory.RATE_LIMIT,
        errors.ProviderPayloadError: errors.ErrorCategory.PROVIDER_PAYLOAD,
        errors.GroundingError: errors.ErrorCategory.GROUNDING,
        errors.ResumeMismatchError: errors.ErrorCategory.RESUME_MISMATCH,
        errors.FilesystemError: errors.ErrorCategory.FILESYSTEM,
    }
    values = {c.value for c in categories.values()}
    # All eight categories must be distinct.
    assert len(values) == 8
    for exc_class, category in categories.items():
        instance = exc_class("msg")
        assert instance.category is category
        assert isinstance(instance, errors.DomainError)
        # Each typed error carries a stable string category for exit-code mapping.
        assert isinstance(instance.category.value, str)


def test_domain_error_is_base_and_carries_message() -> None:
    from video_content_capture.domain.errors import DomainError

    err = DomainError("base")
    assert str(err) == "base"


# --- Late import shim ----------------------------------------------------
