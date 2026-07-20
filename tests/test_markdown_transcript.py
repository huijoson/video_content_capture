"""Focused tests for OpenSpec group 7: canonical transcript and Markdown rendering.

Covers:

* Deterministic Pydantic JSON serialization of the canonical ``Transcript``
  (source metadata, raw/normalized segment text, words, stable IDs,
  timings, anonymous speakers, language) suitable for later adjacent-JSON
  resolution.
* ``markdown/transcript.py`` is a pure deterministic renderer: no provider,
  SDK, network, normalization, or LLM imports/calls. It only renders a
  validated ``Transcript``.
* Traditional Chinese document metadata/headings; a visible ASR fallibility
  warning; one anchored section per segment in the exact form
  ``HH:MM:SS–HH:MM:SS｜講者 X``.
* Anchor identity is stable and derived from canonical segment IDs; quoted
  segment text is escaped so Markdown-sensitive characters or quotes do not
  create extra headings/anchors or alter evidence mapping.
* Timestamps are code-derived, zero-padded, deterministic, nonnegative, and
  within media duration; renderer-level guard tests reject out-of-range
  displayed times.
* Orchestration helper writes ``<stem>.transcript.json`` atomically BEFORE
  ``<stem>.transcript.md``; if Markdown rendering/writing fails, canonical
  JSON remains available and no claim is made that Markdown succeeded.

No provider/network calls are made. Filesystem operations use ``tmp_path``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from video_content_capture.domain.errors import GroundingError
from video_content_capture.domain.models import (
    MediaMetadata,
    StreamInfo,
    Transcript,
    TranscriptSegment,
    Word,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


def _make_media_metadata(
    source_path: Path,
    *,
    duration_seconds: float = 120.0,
) -> MediaMetadata:
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


def _make_segment(
    *,
    segment_id: str = "seg-0001",
    start: float = 0.0,
    end: float = 5.0,
    raw_text: str = "你好",
    normalized_text: str = "你好",
    speaker_label: str = "講者 A",
    words: list[Word] | None = None,
) -> TranscriptSegment:
    return TranscriptSegment(
        segment_id=segment_id,
        start=start,
        end=end,
        raw_text=raw_text,
        normalized_text=normalized_text,
        speaker_label=speaker_label,
        words=words or [],
    )


def _make_transcript(
    *,
    metadata: MediaMetadata | None = None,
    segments: list[TranscriptSegment] | None = None,
    language: str = "zh-TW",
    source_path: Path | None = None,
    duration_seconds: float = 120.0,
) -> Transcript:
    return Transcript(
        metadata=metadata
        or _make_media_metadata(
            source_path or Path("video.mp4"), duration_seconds=duration_seconds
        ),
        segments=segments or [_make_segment(start=0.0, end=5.0)],
        language=language,
    )


# ---------------------------------------------------------------------------
# Provider-neutrality of the renderer module
# ---------------------------------------------------------------------------


def test_markdown_transcript_module_imports_no_provider_or_normalization() -> None:
    """``markdown/transcript.py`` MUST NOT import provider SDKs, network
    libraries, the normalization module, or any LLM/SDK package."""

    import video_content_capture.markdown.transcript as mod

    source = open(mod.__file__, encoding="utf-8").read()
    lowered = source.lower()
    for forbidden in ("assemblyai", "anthropic", "openai", "import requests", "urllib", "httpx"):
        assert forbidden not in lowered, f"renderer must not reference {forbidden!r}"
    # Must not import the normalization module (no normalization logic here).
    assert "from video_content_capture.transcription.normalize" not in source
    assert "import video_content_capture.transcription.normalize" not in source


def test_markdown_transcript_module_does_not_perform_network_calls(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Importing and rendering must not trigger any socket or HTTP activity."""

    import socket

    calls: list[str] = []

    def _refuse(*args: Any, **kwargs: Any) -> Any:
        calls.append("socket")
        raise RuntimeError("network access is forbidden in the renderer")

    monkeypatch.setattr(socket, "socket", _refuse, raising=False)

    from video_content_capture.markdown.transcript import render_transcript_markdown

    transcript = _make_transcript()
    render_transcript_markdown(transcript)
    assert calls == []


# ---------------------------------------------------------------------------
# Timestamp formatting
# ---------------------------------------------------------------------------


def test_format_timestamp_zero_pads_to_hh_mm_ss() -> None:
    from video_content_capture.markdown.transcript import format_timestamp

    assert format_timestamp(0.0) == "00:00:00"
    assert format_timestamp(5.0) == "00:00:05"
    assert format_timestamp(65.0) == "00:01:05"
    assert format_timestamp(3661.0) == "01:01:01"
    assert format_timestamp(35999.0) == "09:59:59"


def test_format_timestamp_truncates_fractional_seconds_deterministically() -> None:
    from video_content_capture.markdown.transcript import format_timestamp

    # Code-derived, deterministic truncation toward zero (no rounding).
    assert format_timestamp(5.9) == "00:00:05"
    assert format_timestamp(65.5) == "00:01:05"
    assert format_timestamp(3661.999) == "01:01:01"


def test_format_timestamp_rejects_negative_input() -> None:
    from video_content_capture.markdown.transcript import format_timestamp

    with pytest.raises(ValueError):
        format_timestamp(-0.1)


# ---------------------------------------------------------------------------
# Anchor derivation (stable, from canonical segment IDs)
# ---------------------------------------------------------------------------


def test_anchor_is_stable_and_derived_from_segment_id() -> None:
    from video_content_capture.markdown.transcript import anchor_for

    # The anchor is a deterministic function of the segment ID only.
    a1 = anchor_for("seg-0001")
    a2 = anchor_for("seg-0001")
    assert a1 == a2
    # Different IDs produce different anchors.
    assert anchor_for("seg-0001") != anchor_for("seg-0002")
    # The anchor is a nonempty string and contains the segment ID in a
    # recognizable, deterministic form (so evidence mapping is unambiguous).
    assert "seg-0001" in a1


def test_anchor_is_idempotent_and_does_not_depend_on_other_state() -> None:
    from video_content_capture.markdown.transcript import anchor_for

    # Calling twice with the same ID returns the same value; the function
    # takes no other argument.
    assert anchor_for("abc") == anchor_for("abc")


# ---------------------------------------------------------------------------
# Markdown rendering — structure, headings, ASR warning
# ---------------------------------------------------------------------------


def test_render_includes_visible_asr_fallibility_warning() -> None:
    from video_content_capture.markdown.transcript import render_transcript_markdown

    md = render_transcript_markdown(_make_transcript())
    # A visible warning that ASR can contain errors MUST appear.
    assert "語音辨識" in md
    assert "錯誤" in md
    # The warning is prominent (a blockquote line).
    assert ">" in md


def test_render_uses_traditional_chinese_document_title() -> None:
    from video_content_capture.markdown.transcript import render_transcript_markdown

    md = render_transcript_markdown(_make_transcript())
    assert "逐字稿" in md


def test_render_includes_source_metadata_block() -> None:
    from video_content_capture.markdown.transcript import render_transcript_markdown

    transcript = _make_transcript(source_path=Path("視野環球財經.mp4"), duration_seconds=2060.0)
    md = render_transcript_markdown(transcript)
    # Source path or container metadata is surfaced.
    assert "視野環球財經.mp4" in md
    # Duration appears as a zero-padded HH:MM:SS.
    assert "00:34:20" in md
    # Language is surfaced.
    assert "zh-TW" in md


def test_render_emits_one_anchored_section_per_segment_in_exact_form() -> None:
    from video_content_capture.markdown.transcript import render_transcript_markdown

    segments = [
        _make_segment(segment_id="seg-0001", start=0.0, end=5.0, speaker_label="講者 A"),
        _make_segment(
            segment_id="seg-0002",
            start=5.0,
            end=12.5,
            speaker_label="講者 B",
        ),
    ]
    transcript = _make_transcript(segments=segments, duration_seconds=120.0)
    md = render_transcript_markdown(transcript)

    # Exact heading form for each segment: HH:MM:SS–HH:MM:SS｜講者 X
    assert "00:00:00–00:00:05｜講者 A" in md
    assert "00:00:05–00:00:12｜講者 B" in md
    # Each heading appears exactly once.
    assert md.count("00:00:00–00:00:05｜講者 A") == 1
    assert md.count("00:00:05–00:00:12｜講者 B") == 1


def test_render_quotes_segment_normalized_text() -> None:
    from video_content_capture.markdown.transcript import render_transcript_markdown

    seg = _make_segment(
        normalized_text="我們今天來談一下利率的走向。",
        raw_text="我们今天来谈一下利率的走向",
    )
    transcript = _make_transcript(segments=[seg], duration_seconds=120.0)
    md = render_transcript_markdown(transcript)
    # The normalized text is quoted (blockquote).
    assert "我們今天來談一下利率的走向。" in md
    # It appears as a quoted line (line begins with '> ').
    quoted_lines = [
        line for line in md.splitlines() if line.startswith("> ") and "我們今天" in line
    ]
    assert quoted_lines, "normalized segment text must be quoted with '> '"


def test_render_preserves_anonymous_speaker_labels() -> None:
    from video_content_capture.markdown.transcript import render_transcript_markdown

    seg = _make_segment(speaker_label="講者 A")
    transcript = _make_transcript(segments=[seg], duration_seconds=120.0)
    md = render_transcript_markdown(transcript)
    assert "講者 A" in md
    # No personal name is invented.
    assert "講者 B" not in md


# ---------------------------------------------------------------------------
# Escaping — Markdown-sensitive characters and quotes
# ---------------------------------------------------------------------------


def test_render_escapes_text_that_would_create_extra_headings() -> None:
    from video_content_capture.markdown.transcript import render_transcript_markdown

    # Text containing a leading '#' would create a spurious heading if
    # rendered verbatim. Quoting prevents that.
    seg = _make_segment(normalized_text="#這不是標題\n而且也不是", raw_text="#這不是標題")
    transcript = _make_transcript(segments=[seg], duration_seconds=120.0)
    md = render_transcript_markdown(transcript)

    # The only level-2 headings MUST be the segment headings. Count '## '
    # heading markers: one per segment (exactly one segment here).
    heading_lines = [line for line in md.splitlines() if line.startswith("## ")]
    assert len(heading_lines) == 1
    assert heading_lines[0].startswith("## 00:00:00–00:00:05｜講者 A")


def test_render_quotes_text_with_blockquote_markers_safely() -> None:
    from video_content_capture.markdown.transcript import render_transcript_markdown

    seg = _make_segment(normalized_text="他說「> 這是引述」結束", raw_text="他說「> 這是引述」結束")
    transcript = _make_transcript(segments=[seg], duration_seconds=120.0)
    md = render_transcript_markdown(transcript)
    # The raw text content is preserved verbatim inside the quote.
    assert "他說「> 這是引述」結束" in md


def test_render_each_segment_anchor_maps_to_exactly_one_segment_id() -> None:
    """Every anchor emitted in the Markdown MUST map to exactly one canonical
    segment ID, and every segment ID MUST have exactly one anchor."""

    from video_content_capture.markdown.transcript import (
        anchor_for,
        render_transcript_markdown,
    )

    segments = [
        _make_segment(segment_id="seg-0001", start=0.0, end=5.0),
        _make_segment(segment_id="seg-0002", start=5.0, end=10.0),
        _make_segment(segment_id="seg-0003", start=10.0, end=15.0),
    ]
    transcript = _make_transcript(segments=segments, duration_seconds=120.0)
    md = render_transcript_markdown(transcript)

    expected_anchors = {anchor_for(seg.segment_id) for seg in segments}
    # Each expected anchor appears exactly once in the rendered output.
    for anchor in expected_anchors:
        assert md.count(anchor) == 1, f"anchor {anchor!r} must appear exactly once"

    # Every HTML anchor tag in the output corresponds to a known segment ID.
    import re

    emitted_anchors = re.findall(r'<a id="([^"]+)"></a>', md)
    assert set(emitted_anchors) == expected_anchors
    # No duplicate anchors.
    assert len(emitted_anchors) == len(set(emitted_anchors)) == len(segments)


# ---------------------------------------------------------------------------
# Renderer-level time guards (within media duration)
# ---------------------------------------------------------------------------


def test_render_rejects_segment_end_beyond_media_duration() -> None:
    from video_content_capture.markdown.transcript import render_transcript_markdown

    seg = _make_segment(start=0.0, end=200.0)
    transcript = _make_transcript(segments=[seg], duration_seconds=120.0)
    with pytest.raises(GroundingError) as exc_info:
        render_transcript_markdown(transcript)
    # The error mentions the out-of-range segment and the media duration.
    msg = str(exc_info.value)
    assert "200" in msg or "duration" in msg.lower()


def test_render_rejects_segment_start_beyond_media_duration() -> None:
    from video_content_capture.markdown.transcript import render_transcript_markdown

    seg = _make_segment(start=150.0, end=160.0)
    transcript = _make_transcript(segments=[seg], duration_seconds=120.0)
    with pytest.raises(GroundingError):
        render_transcript_markdown(transcript)


def test_render_accepts_segment_end_equal_to_media_duration() -> None:
    from video_content_capture.markdown.transcript import render_transcript_markdown

    seg = _make_segment(start=0.0, end=120.0)
    transcript = _make_transcript(segments=[seg], duration_seconds=120.0)
    md = render_transcript_markdown(transcript)
    assert "00:02:00" in md


# ---------------------------------------------------------------------------
# Canonical JSON serialization (deterministic, adjacent-JSON-ready)
# ---------------------------------------------------------------------------


def test_transcript_model_dump_json_is_deterministic_and_round_trips() -> None:
    transcript = _make_transcript(
        segments=[
            _make_segment(
                segment_id="seg-0001",
                start=0.0,
                end=5.0,
                raw_text="我们",
                normalized_text="我們",
                speaker_label="講者 A",
                words=[Word(text="我們", start=0.0, end=0.5, confidence=0.9)],
            )
        ],
        duration_seconds=120.0,
    )
    s1 = transcript.model_dump_json(indent=2)
    s2 = transcript.model_dump_json(indent=2)
    assert s1 == s2  # deterministic
    data = json.loads(s1)
    # Source metadata retained.
    assert data["metadata"]["duration_seconds"] == 120.0
    assert data["metadata"]["source_path"].endswith("video.mp4")
    # Raw and normalized text both retained.
    assert data["segments"][0]["raw_text"] == "我们"
    assert data["segments"][0]["normalized_text"] == "我們"
    # Words retained.
    assert data["segments"][0]["words"][0]["text"] == "我們"
    # Stable ID and timing retained.
    assert data["segments"][0]["segment_id"] == "seg-0001"
    assert data["segments"][0]["start"] == 0.0
    assert data["segments"][0]["end"] == 5.0
    # Anonymous speaker label retained.
    assert data["segments"][0]["speaker_label"] == "講者 A"
    # Language retained.
    assert data["language"] == "zh-TW"
    # Round-trips back into the same model.
    restored = Transcript.model_validate_json(s1)
    assert restored == transcript


def test_transcript_json_retains_audio_stream_metadata() -> None:
    transcript = _make_transcript(duration_seconds=120.0)
    data = json.loads(transcript.model_dump_json())
    audio = data["metadata"]["audio_streams"][0]
    assert audio["codec"] == "aac"
    assert audio["stream_type"] == "audio"
    assert audio["channels"] == 2
    assert audio["sample_rate"] == 44100


# ---------------------------------------------------------------------------
# Orchestration: JSON-before-Markdown write order
# ---------------------------------------------------------------------------


def test_write_transcript_artifacts_writes_json_before_markdown(tmp_path: Path) -> None:
    from video_content_capture.markdown.transcript import write_transcript_artifacts

    transcript = _make_transcript(source_path=tmp_path / "video.mp4", duration_seconds=120.0)
    paths = type(
        "P",
        (),
        {
            "transcript_json": tmp_path / "video.transcript.json",
            "transcript_md": tmp_path / "video.transcript.md",
        },
    )()
    write_transcript_artifacts(paths, transcript)  # type: ignore[arg-type]

    assert paths.transcript_json.is_file()  # type: ignore[attr-defined]
    assert paths.transcript_md.is_file()  # type: ignore[attr-defined]
    # The JSON is canonical and parseable.
    data = json.loads(paths.transcript_json.read_text(encoding="utf-8"))  # type: ignore[attr-defined]
    assert data["segments"][0]["segment_id"] == "seg-0001"
    # The Markdown contains the heading.
    md = paths.transcript_md.read_text(encoding="utf-8")  # type: ignore[attr-defined]
    assert "逐字稿" in md


def test_write_transcript_artifacts_leaves_json_intact_if_markdown_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If Markdown rendering/writing fails, canonical JSON MUST remain
    available and the helper MUST NOT claim Markdown succeeded."""

    from video_content_capture.markdown import transcript as mod

    transcript = _make_transcript(source_path=tmp_path / "video.mp4", duration_seconds=120.0)
    json_path = tmp_path / "video.transcript.json"
    md_path = tmp_path / "video.transcript.md"

    paths = type(
        "P",
        (),
        {"transcript_json": json_path, "transcript_md": md_path},
    )()

    # Sabotage write_markdown so the Markdown step raises after JSON is written.
    def _boom(path: Path, text: str) -> None:
        raise OSError("disk full during markdown write")

    monkeypatch.setattr(mod, "_write_text_atomic", _boom, raising=False)

    with pytest.raises(OSError):
        mod.write_transcript_artifacts(paths, transcript)  # type: ignore[arg-type]

    # Canonical JSON is intact and parseable.
    assert json_path.is_file()
    data = json.loads(json_path.read_text(encoding="utf-8"))
    assert data["segments"][0]["segment_id"] == "seg-0001"
    # No partial Markdown artifact was left at the final path.
    assert not md_path.exists()


def test_write_transcript_artifacts_does_not_duplicate_atomic_write_code(
    tmp_path: Path,
) -> None:
    """The orchestration helper reuses the accepted storage artifact writers
    rather than reimplementing atomic writes."""

    from video_content_capture.markdown import transcript as mod
    from video_content_capture.storage import artifacts

    # The module references the accepted writers (it does not define its own
    # temp+fsync+os.replace routine).
    source = open(mod.__file__, encoding="utf-8").read()
    assert "write_transcript_json" in source or "artifacts.write_transcript_json" in source
    assert "write_markdown" in source or "artifacts.write_markdown" in source
    # It does NOT redefine an atomic-write helper of its own.
    assert "def _atomic_write" not in source
    assert "os.replace" not in source
    # The accepted writers exist and are the ones used.
    assert hasattr(artifacts, "write_transcript_json")
    assert hasattr(artifacts, "write_markdown")
