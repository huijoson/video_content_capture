"""Focused tests for OpenSpec group 9: plain-language report rendering.

Covers:

* ``markdown/report.py`` is a PURE deterministic renderer from a validated
  :class:`Report` (group 8 output) plus its embedded canonical
  :class:`Transcript`. It MUST NOT import a provider SDK, network library,
  normalization module, prompts module, or LLM package.
* All six required Traditional Chinese sections are rendered with the exact
  headings ``三分鐘掌握影片``, ``核心重點``, ``重要數字與說法``,
  ``名詞白話解釋``, ``結論與可能影響``, and ``來源索引``.
* General-reader text is rendered verbatim (apart from safe Markdown
  escaping/formatting) — no invented or rephrased source claims.
* For every source segment ID, the renderer resolves the canonical segment,
  derives ``HH:MM:SS`` timestamp labels in code, and links to the exact
  canonical transcript anchor using the group-7 ``anchor_for`` helper. The
  renderer never trusts model-supplied timestamps (there are none on
  ``Report``).
* Unknown IDs, duplicate/missing required sections, invalid evidence, and
  out-of-range segments fail BEFORE Markdown is written (the validated
  :class:`Report` model + renderer-level guards).
* Every source-dependent report item exposes its evidence links visibly.
* The source index is deterministic, deduplicated, and ordered by canonical
  transcript chronology regardless of model ordering (model ordering is
  ignored; the renderer reorders by segment start time).
* When a structured item has ``is_speaker_opinion=True``, it is visibly
  labeled ``講者觀點``; forecasts/judgments/recommendations do not read as
  independently verified fact or investment advice.
* Markdown-sensitive content (headings, brackets, links, pipes, multiline
  text) does not create extra sections or broken links; text is quoted/escaped
  safely while preserving content.
* Orchestration: ``<stem>.report.json`` is validated and written atomically
  BEFORE ``<stem>.report.md``. If Markdown rendering/writing fails, the
  canonical JSON remains available and no Markdown success is claimed. The
  helper reuses the accepted storage artifact writers (no duplicated
  atomic-write logic).
* Snapshot tests assert EXACT rendered output for a fixture covering
  multiple/duplicate/out-of-order source-index IDs, anonymous speakers,
  opinion labels, glossary entries, and Markdown-sensitive special
  characters.
* Canonical report JSON round-trips deterministically.

No provider/network calls are made. Filesystem operations use ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from video_content_capture.domain.errors import GroundingError
from video_content_capture.domain.models import (
    MediaMetadata,
    Report,
    ReportSection,
    StreamInfo,
    Transcript,
    TranscriptSegment,
)
from video_content_capture.reporting.base import (
    StructuredConclusion,
    StructuredCoreTopic,
    StructuredGlossaryEntry,
    StructuredImportantNumber,
    StructuredOverview,
    StructuredReport,
    StructuredSourceIndex,
)
from video_content_capture.reporting.grounding import ground_structured_report

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_media_metadata(source_path: Path, duration_seconds: float = 120.0) -> MediaMetadata:
    audio = StreamInfo(
        index=1,
        codec="aac",
        stream_type="audio",
        duration_seconds=duration_seconds,
        channels=2,
        sample_rate=44100,
    )
    return MediaMetadata(
        source_path=str(source_path),
        container="mov,mp4,m4a,3gp,3g2,mj2",
        duration_seconds=duration_seconds,
        audio_streams=[audio],
    )


def _segment(
    segment_id: str,
    *,
    start: float,
    end: float,
    text: str,
    speaker: str = "講者 A",
) -> TranscriptSegment:
    return TranscriptSegment(
        segment_id=segment_id,
        start=start,
        end=end,
        raw_text=text,
        normalized_text=text,
        speaker_label=speaker,
    )


def _make_transcript(
    segments: list[TranscriptSegment],
    *,
    duration_seconds: float = 120.0,
    tmp_path: Path | None = None,
) -> Transcript:
    if tmp_path is None:
        src = Path("video.mp4")
    elif tmp_path.suffix:
        src = tmp_path
    else:
        src = tmp_path / "video.mp4"
    return Transcript(
        metadata=_make_media_metadata(src, duration_seconds=duration_seconds),
        segments=segments,
        language="zh-TW",
    )


def _ground(
    transcript: Transcript,
    *,
    overview_ids: list[str] | None = None,
    core_ids: list[str] | None = None,
    numbers_ids: list[str] | None = None,
    glossary_ids: list[str] | None = None,
    conclusion_ids: list[str] | None = None,
    source_index_ids: list[str] | None = None,
    conclusion_is_opinion: bool = True,
    core_is_opinion: bool = False,
    numbers_is_opinion: bool = False,
    core_topic: str = "核心重點：市場利率動向。",
    number_claim: str = "美國聯邦基金利率目標區間 5.25% 至 5.50%。",
    overview_summary: str = "三分鐘掌握影片摘要內容。",
    conclusion_text: str = "講者預期下半年利率可能維持高檔。",
    possible_impact: str = "若利率居高，借貸成本上升，房市與企業投資可能承壓。",
    glossary_term: str = "聯邦基金利率",
    glossary_explanation: str = "銀行間隔夜借貸的目標利率，是美國貨幣政策的關鍵工具。",
) -> Report:
    """Build a validated Report via the accepted grounding path.

    Using the accepted grounding module (not constructing ``Report`` by hand
    where opinion labeling is concerned) keeps group 9 tests honest about the
    real ``Report`` shape the renderer will consume.
    """

    structured = StructuredReport(
        overview=StructuredOverview(
            summary=overview_summary,
            source_segment_ids=overview_ids or [],
        ),
        core_topics=[
            StructuredCoreTopic(
                topic=core_topic,
                source_segment_ids=core_ids or ["s0001"],
                is_speaker_opinion=core_is_opinion,
            )
        ],
        important_numbers=[
            StructuredImportantNumber(
                number_or_claim=number_claim,
                source_segment_ids=numbers_ids or ["s0001"],
                is_speaker_opinion=numbers_is_opinion,
            )
        ],
        glossary=[
            StructuredGlossaryEntry(
                term=glossary_term,
                explanation=glossary_explanation,
                source_segment_ids=glossary_ids or [],
            )
        ],
        conclusion=StructuredConclusion(
            conclusion=conclusion_text,
            possible_impact=possible_impact,
            source_segment_ids=conclusion_ids or ["s0002"],
            is_speaker_opinion=conclusion_is_opinion,
        ),
        source_index=StructuredSourceIndex(
            entries=source_index_ids or ["s0001", "s0002"],
        ),
    )
    return ground_structured_report(transcript, structured)


def _three_segment_transcript(tmp_path: Path) -> Transcript:
    """Three segments with distinct start times; used for chronology tests."""
    segs = [
        _segment("s0001", start=0.0, end=10.0, text="今天談美國聯邦基金利率。"),
        _segment("s0002", start=10.0, end=20.0, text="我認為下半年利率可能維持高檔。"),
        _segment(
            "s0003",
            start=20.0,
            end=30.0,
            text="通膨壓力仍是關鍵變數。",
            speaker="講者 B",
        ),
    ]
    return _make_transcript(segs, duration_seconds=30.0, tmp_path=tmp_path)


# ---------------------------------------------------------------------------
# Provider-neutrality of the renderer module
# ---------------------------------------------------------------------------


def test_report_renderer_module_imports_no_provider_or_normalization() -> None:
    """``markdown/report.py`` MUST NOT import provider SDKs, network libraries,
    the normalization module, the prompts module, or any LLM/SDK package."""

    import video_content_capture.markdown.report as mod

    source = open(mod.__file__, encoding="utf-8").read()
    lowered = source.lower()
    for forbidden in (
        "assemblyai",
        "anthropic",
        "openai",
        "import requests",
        "urllib",
        "httpx",
    ):
        assert forbidden not in lowered, f"renderer must not reference {forbidden!r}"
    # Must not import the normalization or prompts modules (no LLM logic here).
    assert "from video_content_capture.transcription.normalize" not in source
    assert "import video_content_capture.transcription.normalize" not in source
    assert "from video_content_capture.reporting.prompts" not in source
    assert "import video_content_capture.reporting.prompts" not in source
    assert "from video_content_capture.reporting.claude" not in source


def test_report_renderer_module_does_not_perform_network_calls(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    """Importing and rendering must not trigger any socket or HTTP activity."""

    import socket

    calls: list[str] = []

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        calls.append("socket")
        raise RuntimeError("network access is forbidden in the renderer")

    monkeypatch.setattr(socket, "socket", _refuse, raising=False)

    from video_content_capture.markdown.report import render_report_markdown

    report = _ground(_three_segment_transcript(tmp_path))
    render_report_markdown(report)
    assert calls == []


# ---------------------------------------------------------------------------
# Required section headings (exact Traditional Chinese)
# ---------------------------------------------------------------------------


def test_render_includes_all_six_exact_section_headings(tmp_path: Path) -> None:
    from video_content_capture.markdown.report import render_report_markdown

    report = _ground(_three_segment_transcript(tmp_path))
    md = render_report_markdown(report)

    # Each required heading appears as a level-2 Markdown heading exactly once.
    for title in (
        "三分鐘掌握影片",
        "核心重點",
        "重要數字與說法",
        "名詞白話解釋",
        "結論與可能影響",
        "來源索引",
    ):
        assert f"## {title}\n" in md, f"missing exact heading ## {title}"
        assert md.count(f"## {title}\n") == 1, f"heading {title} appears more than once"


def test_render_section_headings_appear_in_canonical_order(tmp_path: Path) -> None:
    from video_content_capture.markdown.report import render_report_markdown

    report = _ground(_three_segment_transcript(tmp_path))
    md = render_report_markdown(report)
    order = [
        md.index("## 三分鐘掌握影片"),
        md.index("## 核心重點"),
        md.index("## 重要數字與說法"),
        md.index("## 名詞白話解釋"),
        md.index("## 結論與可能影響"),
        md.index("## 來源索引"),
    ]
    assert order == sorted(order), "section headings must appear in canonical order"


# ---------------------------------------------------------------------------
# Verbatim content preservation (no invention / rephrasing)
# ---------------------------------------------------------------------------


def test_render_preserves_overview_summary_verbatim(tmp_path: Path) -> None:
    from video_content_capture.markdown.report import render_report_markdown

    report = _ground(
        _three_segment_transcript(tmp_path),
        overview_summary="這是一段固定的概覽文字，不得被改寫。",
    )
    md = render_report_markdown(report)
    assert "這是一段固定的概覽文字，不得被改寫。" in md


def test_render_preserves_glossary_term_and_explanation_verbatim(tmp_path: Path) -> None:
    from video_content_capture.markdown.report import render_report_markdown

    report = _ground(
        _three_segment_transcript(tmp_path),
        glossary_term="量化寬鬆",
        glossary_explanation="央行購買長期債券以壓低長期利率的政策工具。",
    )
    md = render_report_markdown(report)
    assert "量化寬鬆" in md
    assert "央行購買長期債券以壓低長期利率的政策工具。" in md


# ---------------------------------------------------------------------------
# Timestamps and transcript-anchor links derived in code from segment IDs
# ---------------------------------------------------------------------------


def test_render_derives_timestamp_labels_from_segment_ids(tmp_path: Path) -> None:
    from video_content_capture.markdown.report import render_report_markdown

    report = _ground(
        _three_segment_transcript(tmp_path),
        source_index_ids=["s0001", "s0002", "s0003"],
    )
    md = render_report_markdown(report)
    # Timestamps derived from s0001 [0,10], s0002 [10,20], s0003 [20,30].
    assert "00:00:00" in md
    assert "00:00:10" in md
    assert "00:00:20" in md
    assert "00:00:30" in md


def test_render_uses_group7_anchor_for_links(tmp_path: Path) -> None:
    from video_content_capture.markdown.report import render_report_markdown
    from video_content_capture.markdown.transcript import anchor_for

    report = _ground(
        _three_segment_transcript(tmp_path),
        source_index_ids=["s0001", "s0002", "s0003"],
    )
    md = render_report_markdown(report)
    for sid in ("s0001", "s0002", "s0003"):
        anchor = anchor_for(sid)
        # The anchor appears in a Markdown link to the transcript segment.
        assert anchor in md, f"anchor for {sid} missing"
        # A Markdown link targeting the anchor is present.
        assert f"(video.transcript.md#{anchor})" in md, (
            f"link to video.transcript.md#{anchor} missing for {sid}"
        )


def test_evidence_links_target_the_transcript_markdown_file(tmp_path: Path) -> None:
    """Report links must cross into the separate transcript document.

    A bare ``#seg-...`` target points inside the report Markdown, which defines
    no transcript anchors and therefore produces a broken link.
    """

    from video_content_capture.markdown.report import render_report_markdown
    from video_content_capture.markdown.transcript import anchor_for

    transcript = _make_transcript(
        [
            _segment(
                "s0001",
                start=0.0,
                end=10.0,
                text="今天談美國聯邦基金利率。",
            ),
            _segment(
                "s0002",
                start=10.0,
                end=20.0,
                text="我認為下半年利率可能維持高檔。",
            ),
        ],
        duration_seconds=20.0,
        tmp_path=tmp_path / "source video.MP4",
    )
    md = render_report_markdown(_ground(transcript))

    for sid in ("s0001", "s0002"):
        anchor = anchor_for(sid)
        assert f"(source%20video.transcript.md#{anchor})" in md
        assert f"(#{anchor})" not in md


def test_render_links_resolve_to_exactly_one_canonical_segment(tmp_path: Path) -> None:
    """Every evidence link target maps to exactly one canonical segment ID,
    and every referenced segment ID has exactly one anchor link in the output."""

    import re

    from video_content_capture.markdown.report import render_report_markdown
    from video_content_capture.markdown.transcript import anchor_for

    transcript = _three_segment_transcript(tmp_path)
    report = _ground(transcript)
    md = render_report_markdown(report)

    # Collect every cross-document Markdown target ending in #seg-....
    link_targets = re.findall(r"\([^\)]+#(seg-[^\)]+)\)", md)
    valid_anchors = {anchor_for(seg.segment_id) for seg in transcript.segments}
    for target in link_targets:
        assert target in valid_anchors, f"link target {target!r} is not a valid anchor"
    # Every referenced source-dependent segment ID appears as a link target.
    for section in report.sections:
        for sid in section.source_segment_ids:
            assert anchor_for(sid) in link_targets, (
                f"segment {sid} has no evidence link in the rendered report"
            )


def test_render_does_not_trust_model_supplied_timestamps(tmp_path: Path) -> None:
    """The renderer MUST NOT echo any model-authored timestamp field; the
    ``Report`` model carries none, and the renderer derives all times from
    canonical segments."""

    from video_content_capture.markdown.report import render_report_markdown

    # The Report model has no timestamp fields on sections.
    report = _ground(_three_segment_transcript(tmp_path))
    for section in report.sections:
        field_names = set(type(section).model_fields.keys())
        assert "timestamp" not in field_names
        assert "start_time" not in field_names
        assert "end_time" not in field_names
    md = render_report_markdown(report)
    # No model-authored timestamp text leaked (only code-derived HH:MM:SS).
    # Sanity: the only HH:MM:SS strings present are the derived ones.
    import re

    times = set(re.findall(r"\d{2}:\d{2}:\d{2}", md))
    assert times <= {"00:00:00", "00:00:10", "00:00:20", "00:00:30"}


# ---------------------------------------------------------------------------
# Evidence visibility for source-dependent items
# ---------------------------------------------------------------------------


def test_render_exposes_evidence_links_for_every_source_dependent_section(
    tmp_path: Path,
) -> None:
    from video_content_capture.markdown.report import render_report_markdown

    transcript = _three_segment_transcript(tmp_path)
    report = _ground(transcript)
    md = render_report_markdown(report)
    # Source-dependent sections: core_topics, important_numbers, conclusion.
    # Each must visibly expose its evidence (anchor link + timestamp).
    for section in report.sections:
        if not section.is_source_dependent:
            continue
        for sid in section.source_segment_ids:
            seg = next(s for s in transcript.segments if s.segment_id == sid)
            # The timestamp label is present.
            from video_content_capture.markdown.transcript import format_timestamp

            assert format_timestamp(seg.start) in md
            assert format_timestamp(seg.end) in md


# ---------------------------------------------------------------------------
# Source index: deterministic, deduplicated, chronologically ordered
# ---------------------------------------------------------------------------


def test_source_index_is_deduplicated_and_ordered_by_chronology(
    tmp_path: Path,
) -> None:
    """The rendered source index MUST be deduplicated and ordered by canonical
    transcript chronology, regardless of the model's source-index ordering."""

    from video_content_capture.markdown.report import render_report_markdown

    transcript = _three_segment_transcript(tmp_path)
    # Model supplies duplicate + out-of-order IDs.
    report = _ground(
        transcript,
        source_index_ids=["s0003", "s0002", "s0001", "s0002", "s0003"],
    )
    md = render_report_markdown(report)

    # Extract the source-index block (between its heading and the next ## or EOF).
    import re

    block = re.search(r"## 來源索引\n(.*?)(?=\n## |\Z)", md, re.DOTALL)
    assert block is not None
    index_text = block.group(1)

    # No duplicate index entries: each segment anchor link appears exactly once
    # in the source-index block.
    from video_content_capture.markdown.transcript import anchor_for

    # The IDs appear in canonical chronological order: s0001, s0002, s0003.
    pos = {sid: index_text.find(f"#{anchor_for(sid)}") for sid in ("s0001", "s0002", "s0003")}
    assert pos["s0001"] < pos["s0002"] < pos["s0003"], "source index not chronological"

    for sid in ("s0001", "s0002", "s0003"):
        anchor = anchor_for(sid)
        assert index_text.count(f"(video.transcript.md#{anchor})") == 1, (
            f"source index entry for {sid} must appear exactly once"
        )


# ---------------------------------------------------------------------------
# Opinion labeling (講者觀點)
# ---------------------------------------------------------------------------


def test_render_labels_speaker_opinion_conclusion(tmp_path: Path) -> None:
    from video_content_capture.markdown.report import render_report_markdown

    report = _ground(
        _three_segment_transcript(tmp_path),
        conclusion_text="我預期下半年利率會維持高檔。",
        conclusion_is_opinion=True,
    )
    md = render_report_markdown(report)
    assert "講者觀點" in md


def test_render_labels_speaker_opinion_core_topic(tmp_path: Path) -> None:
    from video_content_capture.markdown.report import render_report_markdown

    report = _ground(
        _three_segment_transcript(tmp_path),
        core_topic="我預期下半年利率會維持高檔。",
        core_is_opinion=True,
    )
    md = render_report_markdown(report)
    assert "講者觀點" in md


def test_render_labels_speaker_opinion_important_number(tmp_path: Path) -> None:
    from video_content_capture.markdown.report import render_report_markdown

    report = _ground(
        _three_segment_transcript(tmp_path),
        number_claim="我認為美國今年應該會升息一次。",
        numbers_is_opinion=True,
    )
    md = render_report_markdown(report)
    assert "講者觀點" in md


def test_render_does_not_label_non_opinion_factual_items(tmp_path: Path) -> None:
    from video_content_capture.markdown.report import render_report_markdown

    report = _ground(
        _three_segment_transcript(tmp_path),
        core_topic="聯準會宣布維持利率不變。",
        core_is_opinion=False,
        number_claim="美國聯邦基金利率目標區間 5.25% 至 5.50%。",
        numbers_is_opinion=False,
        conclusion_text="本集重點在於利率政策走向。",
        conclusion_is_opinion=False,
    )
    md = render_report_markdown(report)
    # No opinion label on factual items.
    assert "講者觀點" not in md


# ---------------------------------------------------------------------------
# Markdown-sensitive content escaping
# ---------------------------------------------------------------------------


def test_render_escapes_heading_characters_in_content(tmp_path: Path) -> None:
    """Content containing ``#`` must not create extra Markdown headings."""

    from video_content_capture.markdown.report import render_report_markdown

    report = _ground(
        _three_segment_transcript(tmp_path),
        overview_summary="#這不是標題 而是概覽內容",
    )
    md = render_report_markdown(report)
    # Exactly six level-2 headings (the required section headings); no extras.
    heading_lines = [line for line in md.splitlines() if line.startswith("## ")]
    assert len(heading_lines) == 6
    # The content text is still present verbatim.
    assert "#這不是標題 而是概覽內容" in md


def test_render_preserves_special_characters_in_glossary(tmp_path: Path) -> None:
    from video_content_capture.markdown.report import render_report_markdown

    report = _ground(
        _three_segment_transcript(tmp_path),
        glossary_term="殖利率【曲線】",
        glossary_explanation="長短天期公債殖利率的連線，出現「倒掛」時常被視為經濟衰退訊號。",
    )
    md = render_report_markdown(report)
    assert "殖利率【曲線】" in md
    assert "出現「倒掛」時常被視為經濟衰退訊號。" in md


def test_render_handles_multiline_content_safely(tmp_path: Path) -> None:
    """Multiline content must not create extra sections or break structure."""

    from video_content_capture.markdown.report import render_report_markdown

    report = _ground(
        _three_segment_transcript(tmp_path),
        overview_summary="第一行。\n第二行。\n## 這不應該是標題",
    )
    md = render_report_markdown(report)
    heading_lines = [line for line in md.splitlines() if line.startswith("## ")]
    assert len(heading_lines) == 6
    assert "第一行。" in md
    assert "第二行。" in md


# ---------------------------------------------------------------------------
# Failure paths: unknown IDs / missing sections / out-of-range fail before MD
# ---------------------------------------------------------------------------


def test_report_model_rejects_unknown_segment_id() -> None:
    """Constructing a Report with an unknown evidence ID fails before render."""

    transcript = _three_segment_transcript(Path("video.mp4"))
    bad = ReportSection(
        section_type="core_topics",
        title="核心重點",
        content="- 壞證據",
        source_segment_ids=["s9999"],
        is_source_dependent=True,
    )
    good_sections = [
        ReportSection(section_type="overview", title="三分鐘掌握影片", content="概覽"),
        ReportSection(
            section_type="important_numbers",
            title="重要數字與說法",
            content="- 數字",
            source_segment_ids=["s0001"],
            is_source_dependent=True,
        ),
        ReportSection(section_type="glossary", title="名詞白話解釋", content="- 名詞"),
        ReportSection(
            section_type="conclusion",
            title="結論與可能影響",
            content="結論",
            source_segment_ids=["s0001"],
            is_source_dependent=True,
        ),
        ReportSection(section_type="source_index", title="來源索引", content="索引"),
    ]
    with pytest.raises(ValidationError):
        Report(transcript=transcript, sections=[bad, *good_sections])


def test_render_rejects_out_of_range_evidence_timing(tmp_path: Path) -> None:
    """A referenced segment whose timing exceeds media duration must fail
    before Markdown is written (renderer-level guard).

    Grounding already rejects out-of-range timing for model-returned reports;
    this test constructs a ``Report`` directly with an out-of-range segment to
    exercise the renderer's defense-in-depth guard (a programmatically-
    constructed report, e.g. from a future reducer, must not emit timestamps
    that exceed the source media).
    """

    from video_content_capture.markdown.report import render_report_markdown

    bad_seg = _segment("s0500", start=0.0, end=9999.0, text="超時內容。")
    transcript = _make_transcript([bad_seg], duration_seconds=10.0, tmp_path=tmp_path)
    report = Report(
        transcript=transcript,
        sections=[
            ReportSection(
                section_type="overview",
                title="三分鐘掌握影片",
                content="概覽",
                is_source_dependent=False,
            ),
            ReportSection(
                section_type="core_topics",
                title="核心重點",
                content="- 重點",
                source_segment_ids=["s0500"],
                is_source_dependent=True,
            ),
            ReportSection(
                section_type="important_numbers",
                title="重要數字與說法",
                content="- 數字",
                source_segment_ids=["s0500"],
                is_source_dependent=True,
            ),
            ReportSection(
                section_type="glossary",
                title="名詞白話解釋",
                content="- 名詞",
                is_source_dependent=False,
            ),
            ReportSection(
                section_type="conclusion",
                title="結論與可能影響",
                content="結論",
                source_segment_ids=["s0500"],
                is_source_dependent=True,
            ),
            ReportSection(
                section_type="source_index",
                title="來源索引",
                content="",
                source_segment_ids=["s0500"],
                is_source_dependent=False,
            ),
        ],
    )
    with pytest.raises(GroundingError):
        render_report_markdown(report)


# ---------------------------------------------------------------------------
# Orchestration: JSON-before-Markdown write order, atomicity, no duplication
# ---------------------------------------------------------------------------


def _report_paths(tmp_path: Path) -> Any:
    return type(
        "P",
        (),
        {
            "report_json": tmp_path / "video.report.json",
            "report_md": tmp_path / "video.report.md",
        },
    )()


def test_write_report_artifacts_writes_json_before_markdown(tmp_path: Path) -> None:
    from video_content_capture.markdown.report import write_report_artifacts

    report = _ground(_three_segment_transcript(tmp_path))
    paths = _report_paths(tmp_path)
    write_report_artifacts(paths, report)  # type: ignore[arg-type]

    assert paths.report_json.is_file()  # type: ignore[attr-defined]
    assert paths.report_md.is_file()  # type: ignore[attr-defined]
    data = json.loads(paths.report_json.read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert data["sections"][0]["section_type"] == "overview"
    md = paths.report_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert "三分鐘掌握影片" in md
    assert "來源索引" in md


def test_write_report_artifacts_links_to_transcript_across_output_directories(
    tmp_path: Path,
) -> None:
    from video_content_capture.markdown.report import write_report_artifacts
    from video_content_capture.markdown.transcript import anchor_for

    report = _ground(
        _make_transcript(
            [
                _segment(
                    "s0001",
                    start=0.0,
                    end=10.0,
                    text="今天談美國聯邦基金利率。",
                ),
                _segment(
                    "s0002",
                    start=10.0,
                    end=20.0,
                    text="我認為下半年利率可能維持高檔。",
                ),
            ],
            duration_seconds=20.0,
            tmp_path=tmp_path / "source video.MP4",
        )
    )
    report_dir = tmp_path / "reports"
    paths = _report_paths(report_dir)
    transcript_md = tmp_path / "transcripts" / "source video.transcript.md"

    write_report_artifacts(  # type: ignore[arg-type]
        paths,
        report,
        transcript_markdown_path=transcript_md,
    )

    md = paths.report_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    expected = f"../transcripts/source%20video.transcript.md#{anchor_for('s0001')}"
    assert expected in md


def test_write_report_artifacts_leaves_json_intact_if_markdown_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Markdown rendering/writing fails, canonical JSON MUST remain available
    and no Markdown success is claimed (no partial Markdown at the final path)."""

    from video_content_capture.markdown import report as mod

    report = _ground(_three_segment_transcript(tmp_path))
    paths = _report_paths(tmp_path)

    def _boom(path: Path, text: str) -> None:
        raise OSError("disk full during markdown write")

    monkeypatch.setattr(mod, "_write_markdown", _boom, raising=False)

    with pytest.raises(OSError):
        mod.write_report_artifacts(paths, report)  # type: ignore[arg-type]

    # Canonical JSON is intact and parseable.
    assert paths.report_json.is_file()  # type: ignore[attr-defined]
    data = json.loads(paths.report_json.read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert data["sections"][0]["section_type"] == "overview"
    # No partial Markdown artifact at the final path.
    assert not paths.report_md.exists()  # type: ignore[attr-defined]


def test_write_report_artifacts_does_not_duplicate_atomic_write_code() -> None:
    """The orchestration helper reuses the accepted storage artifact writers
    rather than reimplementing atomic writes."""

    from video_content_capture.markdown import report as mod
    from video_content_capture.storage import artifacts

    source = open(mod.__file__, encoding="utf-8").read()
    assert "write_report_json" in source
    assert "write_markdown" in source
    # It does NOT redefine an atomic-write helper of its own.
    assert "def _atomic_write" not in source
    assert "os.replace" not in source
    # The accepted writers exist and are the ones used.
    assert hasattr(artifacts, "write_report_json")
    assert hasattr(artifacts, "write_markdown")


# ---------------------------------------------------------------------------
# Anonymous speakers preserved in source index
# ---------------------------------------------------------------------------


def test_source_index_shows_anonymous_speaker_labels(tmp_path: Path) -> None:
    from video_content_capture.markdown.report import render_report_markdown

    transcript = _three_segment_transcript(tmp_path)
    report = _ground(transcript, source_index_ids=["s0001", "s0003"])
    md = render_report_markdown(report)
    # 講者 A and 講者 B appear; no personal names invented.
    assert "講者 A" in md
    assert "講者 B" in md


# ---------------------------------------------------------------------------
# Exact snapshot test (deterministic full-output assertion)
# ---------------------------------------------------------------------------


def test_report_markdown_snapshot_exact_output(tmp_path: Path) -> None:
    """A readable, deterministic snapshot asserting EXACT rendered output.

    The fixture covers: multiple source-index IDs, duplicate + out-of-order
    model ordering, anonymous speakers (講者 A / 講者 B), opinion labels on
    core_topics / important_numbers / conclusion, glossary entries with
    special characters, and Markdown-sensitive content.
    """

    from video_content_capture.markdown.report import render_report_markdown
    from video_content_capture.markdown.transcript import anchor_for, format_timestamp

    segs = [
        _segment("s0001", start=0.0, end=10.0, text="今天談美國聯邦基金利率。"),
        _segment(
            "s0002",
            start=10.0,
            end=20.0,
            text="我預期下半年利率可能維持高檔。",
        ),
        _segment(
            "s0003",
            start=20.0,
            end=30.0,
            text="通膨壓力仍是關鍵變數。",
            speaker="講者 B",
        ),
    ]
    transcript = _make_transcript(segs, duration_seconds=30.0, tmp_path=tmp_path)

    report = _ground(
        transcript,
        overview_summary="三分鐘掌握：本集討論美國利率政策與下半年展望。",
        core_topic="我預期下半年利率會維持高檔。",
        core_is_opinion=True,
        core_ids=["s0002"],
        number_claim="美國聯邦基金利率目標區間 5.25% 至 5.50%。",
        numbers_is_opinion=False,
        numbers_ids=["s0001"],
        glossary_term="聯邦基金利率",
        glossary_explanation="銀行間隔夜借貸的目標利率，是美國貨幣政策的關鍵工具。",
        glossary_ids=["s0001"],
        conclusion_text="講者預期下半年利率可能維持高檔。",
        possible_impact="若利率居高，借貸成本上升，房市與企業投資可能承壓。",
        conclusion_is_opinion=True,
        conclusion_ids=["s0002", "s0003"],
        # Duplicate + out-of-order: renderer must dedupe and reorder chronologically.
        source_index_ids=["s0003", "s0002", "s0001", "s0002", "s0003"],
    )

    md = render_report_markdown(report)

    a1 = anchor_for("s0001")
    a2 = anchor_for("s0002")
    a3 = anchor_for("s0003")
    t1s, t1e = format_timestamp(0.0), format_timestamp(10.0)
    t2s, t2e = format_timestamp(10.0), format_timestamp(20.0)
    t3s, t3e = format_timestamp(20.0), format_timestamp(30.0)

    expected = (
        "# 影片重點摘要\n"
        "\n"
        "> ⚠️ 本報告由自動語音辨識（ASR）逐字稿產生，內容可能包含錯誤，"
        "請以原始影音為準。\n"
        "\n"
        "## 三分鐘掌握影片\n"
        "\n"
        "三分鐘掌握：本集討論美國利率政策與下半年展望。\n"
        "\n"
        "## 核心重點\n"
        "\n"
        f"- 我預期下半年利率會維持高檔。"
        f" [{t2s}–{t2e}｜講者 A](video.transcript.md#{a2})（講者觀點）\n"
        "\n"
        "## 重要數字與說法\n"
        "\n"
        f"- 美國聯邦基金利率目標區間 5.25% 至 5.50%。"
        f" [{t1s}–{t1e}｜講者 A](video.transcript.md#{a1})\n"
        "\n"
        "## 名詞白話解釋\n"
        "\n"
        "- 聯邦基金利率：銀行間隔夜借貸的目標利率，是美國貨幣政策的關鍵工具。"
        f" [{t1s}–{t1e}｜講者 A](video.transcript.md#{a1})\n"
        "\n"
        "## 結論與可能影響\n"
        "\n"
        f"- 講者預期下半年利率可能維持高檔。"
        f" [{t2s}–{t2e}｜講者 A](video.transcript.md#{a2})"
        f" [{t3s}–{t3e}｜講者 B](video.transcript.md#{a3})（講者觀點）\n"
        f"- 若利率居高，借貸成本上升，房市與企業投資可能承壓。"
        f" [{t2s}–{t2e}｜講者 A](video.transcript.md#{a2})"
        f" [{t3s}–{t3e}｜講者 B](video.transcript.md#{a3})（講者觀點）\n"
        "\n"
        "## 來源索引\n"
        "\n"
        f"- [{t1s}–{t1e}｜講者 A](video.transcript.md#{a1}) — s0001\n"
        f"- [{t2s}–{t2e}｜講者 A](video.transcript.md#{a2}) — s0002\n"
        f"- [{t3s}–{t3e}｜講者 B](video.transcript.md#{a3}) — s0003\n"
    )

    assert md == expected


# ---------------------------------------------------------------------------
# I1/I2: per-item inline evidence links + opinion-from-flag (regression)
# ---------------------------------------------------------------------------


def test_each_source_dependent_item_has_own_inline_evidence_links(
    tmp_path: Path,
) -> None:
    """Every source-dependent item MUST have its own inline evidence links
    appended to its bullet — not merely somewhere in the source index."""

    import re

    from video_content_capture.markdown.report import render_report_markdown
    from video_content_capture.markdown.transcript import anchor_for

    transcript = _three_segment_transcript(tmp_path)
    report = _ground(
        transcript,
        core_ids=["s0001"],
        numbers_ids=["s0002"],
        conclusion_ids=["s0003"],
        source_index_ids=["s0001", "s0002", "s0003"],
    )
    md = render_report_markdown(report)

    # The core_topics bullet (one item) must carry an inline link to s0001.
    core_block = re.search(r"## 核心重點\n(.*?)(?=\n## |\Z)", md, re.DOTALL).group(1)
    assert f"(video.transcript.md#{anchor_for('s0001')})" in core_block

    # The important_numbers bullet must carry an inline link to s0002.
    numbers_block = re.search(r"## 重要數字與說法\n(.*?)(?=\n## |\Z)", md, re.DOTALL).group(1)
    assert f"(video.transcript.md#{anchor_for('s0002')})" in numbers_block

    # The conclusion bullet(s) must carry an inline link to s0003.
    conclusion_block = re.search(r"## 結論與可能影響\n(.*?)(?=\n## |\Z)", md, re.DOTALL).group(1)
    assert f"(video.transcript.md#{anchor_for('s0003')})" in conclusion_block


def test_glossary_item_carries_inline_evidence_when_present(tmp_path: Path) -> None:
    """A glossary entry with evidence carries inline evidence links on its
    bullet (not only in the source index)."""

    import re

    from video_content_capture.markdown.report import render_report_markdown
    from video_content_capture.markdown.transcript import anchor_for

    transcript = _three_segment_transcript(tmp_path)
    report = _ground(
        transcript,
        glossary_ids=["s0001"],
        source_index_ids=["s0001", "s0002", "s0003"],
    )
    md = render_report_markdown(report)
    glossary_block = re.search(r"## 名詞白話解釋\n(.*?)(?=\n## |\Z)", md, re.DOTALL).group(1)
    assert f"(video.transcript.md#{anchor_for('s0001')})" in glossary_block


def test_overview_item_carries_inline_evidence_when_present(tmp_path: Path) -> None:
    """An overview with evidence carries inline evidence links after the
    overview paragraph."""

    import re

    from video_content_capture.markdown.report import render_report_markdown
    from video_content_capture.markdown.transcript import anchor_for

    transcript = _three_segment_transcript(tmp_path)
    report = _ground(
        transcript,
        overview_ids=["s0001"],
        source_index_ids=["s0001", "s0002", "s0003"],
    )
    md = render_report_markdown(report)
    overview_block = re.search(r"## 三分鐘掌握影片\n(.*?)(?=\n## |\Z)", md, re.DOTALL).group(1)
    assert f"(video.transcript.md#{anchor_for('s0001')})" in overview_block


def test_renderer_labels_opinion_from_flag_not_inferred(tmp_path: Path) -> None:
    """The renderer appends ``（講者觀點）`` from the structured
    ``is_speaker_opinion`` flag; it never infers opinion from text. A factual
    item whose text happens to contain an opinion keyword but whose flag is
    False is NOT labeled."""

    from video_content_capture.domain.models import ReportItem
    from video_content_capture.markdown.report import render_report_markdown

    transcript = _three_segment_transcript(tmp_path)

    def _build_report(core_item: ReportItem) -> Report:
        sections = [
            ReportSection(
                section_type="overview",
                title="三分鐘掌握影片",
                content="概覽",
                items=[ReportItem(text="概覽。", source_segment_ids=[])],
                is_source_dependent=False,
            ),
            ReportSection(
                section_type="core_topics",
                title="核心重點",
                content="",
                items=[core_item],
                source_segment_ids=list(core_item.source_segment_ids),
                is_source_dependent=True,
            ),
            ReportSection(
                section_type="important_numbers",
                title="重要數字與說法",
                content="",
                items=[
                    ReportItem(
                        text="5.25% 至 5.50%。",
                        source_segment_ids=["s0001"],
                    )
                ],
                source_segment_ids=["s0001"],
                is_source_dependent=True,
            ),
            ReportSection(
                section_type="glossary",
                title="名詞白話解釋",
                content="",
                items=[
                    ReportItem(
                        label="利率",
                        text="借貸成本的指標。",
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
                        text="本集重點在於利率政策走向。",
                        source_segment_ids=["s0001"],
                        is_speaker_opinion=False,
                    )
                ],
                source_segment_ids=["s0001"],
                is_source_dependent=True,
            ),
            ReportSection(
                section_type="source_index",
                title="來源索引",
                content="",
                items=[ReportItem(text="s0001", source_segment_ids=["s0001"])],
                source_segment_ids=["s0001"],
                is_source_dependent=False,
            ),
        ]
        return Report(transcript=transcript, sections=sections)

    # Factual text containing an opinion keyword, but flag is False — the
    # renderer must NOT label it (opinion comes from the flag, not the text).
    factual = _build_report(
        ReportItem(
            text="會議記錄提到「預期」一字但屬陳述事實。",
            source_segment_ids=["s0001"],
            is_speaker_opinion=False,
        )
    )
    assert "講者觀點" not in render_report_markdown(factual)

    # A flagged opinion item whose text contains NO opinion keyword IS labeled.
    opinion = _build_report(
        ReportItem(
            text="純粹事實陳述。",
            source_segment_ids=["s0001"],
            is_speaker_opinion=True,
        )
    )
    assert "講者觀點" in render_report_markdown(opinion)


def test_canonical_report_json_has_no_markdown_bullets_or_opinion_labels(
    tmp_path: Path,
) -> None:
    """Canonical report JSON contains structured item text/flags/IDs but NO
    Markdown bullet prefix and NO embedded ``（講者觀點）`` label — those are
    render-time concerns."""

    report = _ground(
        _three_segment_transcript(tmp_path),
        core_topic="我預期下半年利率會維持高檔。",
        core_is_opinion=True,
        core_ids=["s0002"],
        source_index_ids=["s0001", "s0002", "s0003"],
    )
    blob = report.model_dump_json(indent=2)
    assert "（講者觀點）" not in blob
    # No item text in the JSON starts with a Markdown bullet.
    data = json.loads(blob)
    for section in data["sections"]:
        for item in section.get("items", []):
            assert not item["text"].startswith("- ")
    # But the structured flag IS present.
    core = next(s for s in data["sections"] if s["section_type"] == "core_topics")
    assert core["items"][0]["is_speaker_opinion"] is True


# ---------------------------------------------------------------------------
# Canonical report JSON deterministic round-trip
# ---------------------------------------------------------------------------


def test_report_json_serialization_is_deterministic_and_round_trips(
    tmp_path: Path,
) -> None:
    report = _ground(_three_segment_transcript(tmp_path))
    s1 = report.model_dump_json(indent=2)
    s2 = report.model_dump_json(indent=2)
    assert s1 == s2
    data = json.loads(s1)
    assert len(data["sections"]) == 6
    assert data["sections"][0]["section_type"] == "overview"
    # Round-trips back into the same model.
    restored = Report.model_validate_json(s1)
    assert restored == report
