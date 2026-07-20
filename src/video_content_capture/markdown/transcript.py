"""Deterministic Traditional-Chinese Markdown renderer for the canonical transcript.

This module is a PURE RENDERER. It:

* Takes a validated :class:`Transcript` (the model's own validators already
  guarantee ordered, unique, nonnegative segment IDs and timing).
* Derives zero-padded ``HH:MM:SS`` timestamps from segment timings in code —
  the model never supplies authoritative timestamps.
* Emits one anchored ``##`` section per segment in the exact form
  ``HH:MM:SS–HH:MM:SS｜講者 X``.
* Quotes each segment's normalized text as a Markdown blockquote so text
  containing ``#``, ``>``, or other Markdown-sensitive characters cannot
  create extra headings/anchors or alter evidence mapping.
* Adds a visible warning that automated speech recognition can contain
  errors.

It MUST NOT import a provider SDK, the network, or the normalization module.
It MUST NOT redefine atomic-write helpers; it reuses the accepted
``storage.artifacts`` writers. It MUST NOT call a cloud provider or LLM.

Renderer-level guards: even though the domain model already enforces
nonnegative ordered timing, the renderer additionally asserts that every
displayed time lies within media duration (``[0, metadata.duration_seconds]``).
This keeps a future caller that constructs a ``Transcript`` programmatically
(e.g. a reducer merging chunks) from emitting timestamps that exceed the
source media.
"""

from __future__ import annotations

from pathlib import Path
from typing import Protocol

from video_content_capture.domain.errors import GroundingError
from video_content_capture.domain.models import Transcript, TranscriptSegment
from video_content_capture.storage.artifacts import write_markdown, write_transcript_json

# Visible Traditional Chinese warning that ASR can contain errors.
_ASR_WARNING_LINE = "> ⚠️ 本逐字稿由自動語音辨識（ASR）產生，內容可能包含錯誤，請以原始影音為準。"

# Document title (Traditional Chinese).
_DOCUMENT_TITLE = "逐字稿"


# --- Protocol for artifact paths ------------------------------------------


class _TranscriptArtifactPaths(Protocol):
    """Minimal path surface the orchestration helper needs.

    Implemented by :class:`storage.paths.ArtifactPaths`; declared here as a
    Protocol so the renderer does not import the concrete paths dataclass
    (keeping the renderer free of storage-internal concerns).
    """

    transcript_json: Path
    transcript_md: Path


# --- Timestamp formatting -------------------------------------------------


def format_timestamp(seconds: float) -> str:
    """Return a deterministic, zero-padded ``HH:MM:SS`` string for ``seconds``.

    Fractional seconds are truncated toward zero (no rounding) so the output
    is a pure, code-derived projection of the timing. Negative input is
    rejected with :class:`ValueError` — the renderer never emits negative
    times.
    """

    if seconds < 0:
        raise ValueError(f"timestamp must be nonnegative, got {seconds!r}")
    total = int(seconds)  # truncation toward zero
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


# --- Anchor derivation ----------------------------------------------------


def anchor_for(segment_id: str) -> str:
    """Return a stable anchor string derived solely from ``segment_id``.

    The anchor is a deterministic function of the canonical segment ID, so a
    report's evidence link maps to exactly one canonical segment. The ID is
    embedded verbatim and prefixed so the anchor is a valid HTML id and is
    recognizable in the rendered Markdown.
    """

    return f"seg-{segment_id}"


# --- Rendering ------------------------------------------------------------


def _validate_displayed_times(transcript: Transcript) -> None:
    """Assert every segment's displayed start/end lies within media duration.

    The domain model already guarantees nonnegative, ordered timing; this
    guard additionally bounds displayed times to ``[0, duration_seconds]`` so
    a programmatically-constructed transcript (e.g. a reducer) cannot emit
    times that exceed the source media. Raises :class:`GroundingError` (a
    stable typed domain error) on violation so callers can classify it.
    """

    duration = transcript.metadata.duration_seconds
    for seg in transcript.segments:
        if seg.start > duration:
            raise GroundingError(
                f"segment {seg.segment_id!r} start {seg.start} exceeds media duration {duration}",
                details={"segment_id": seg.segment_id, "start": seg.start, "duration": duration},
            )
        if seg.end > duration:
            raise GroundingError(
                f"segment {seg.segment_id!r} end {seg.end} exceeds media duration {duration}",
                details={"segment_id": seg.segment_id, "end": seg.end, "duration": duration},
            )


def _heading_for(seg: TranscriptSegment) -> str:
    """Render the per-segment heading in the exact form
    ``HH:MM:SS–HH:MM:SS｜講者 X`` with an HTML anchor."""

    start = format_timestamp(seg.start)
    end = format_timestamp(seg.end)
    anchor = anchor_for(seg.segment_id)
    return f'<a id="{anchor}"></a>\n## {start}–{end}｜{seg.speaker_label}'


def _quote_segment_text(text: str) -> str:
    """Quote ``text`` as a Markdown blockquote, line by line.

    Each line is prefixed with ``> `` so Markdown-sensitive characters
    (``#``, ``>``, leading whitespace, etc.) inside the text cannot create
    extra headings, blockquotes, or code blocks. Empty lines become ``>``
    separators to preserve paragraph breaks within the quote.
    """

    if not text:
        return ">"
    lines = text.splitlines() or [text]
    quoted: list[str] = []
    for line in lines:
        if line == "":
            quoted.append(">")
        else:
            quoted.append(f"> {line}")
    return "\n".join(quoted)


def _render_segment(seg: TranscriptSegment) -> str:
    """Render one segment's anchored section."""

    heading = _heading_for(seg)
    body = _quote_segment_text(seg.normalized_text)
    return f"{heading}\n\n{body}"


def _render_metadata_block(transcript: Transcript) -> str:
    """Render the source-metadata header block."""

    md = transcript.metadata
    duration = format_timestamp(md.duration_seconds)
    lines = [
        f"# {_DOCUMENT_TITLE}",
        "",
        _ASR_WARNING_LINE,
        "",
        "- 來源檔案：" + md.source_path,
        "- 容器格式：" + md.container,
        "- 影片長度：" + duration,
        "- 語言：" + transcript.language,
        "- 段落數：" + str(len(transcript.segments)),
    ]
    return "\n".join(lines)


def render_transcript_markdown(transcript: Transcript) -> str:
    """Render a validated :class:`Transcript` to a deterministic Markdown string.

    Pure: no provider/network/normalization/LLM calls. Timestamps are
    code-derived; anchors are derived from canonical segment IDs; segment
    text is quoted so it cannot create extra headings/anchors.
    """

    _validate_displayed_times(transcript)
    parts: list[str] = [_render_metadata_block(transcript), ""]
    for seg in transcript.segments:
        parts.append(_render_segment(seg))
        parts.append("")
    # Join with a single newline per line; trailing empty string yields a
    # single trailing newline.
    return "\n".join(parts).rstrip("\n") + "\n"


# --- Orchestration: JSON before Markdown ----------------------------------


def _write_text_atomic(path: Path, text: str) -> None:
    """Indirection point used by :func:`write_transcript_artifacts`.

    This function exists ONLY so tests can monkeypatch the Markdown write to
    simulate a failure AFTER JSON is written. It delegates to the accepted
    :func:`video_content_capture.storage.artifacts.write_markdown` writer —
    it does NOT reimplement atomic writes.
    """

    write_markdown(path, text)


def write_transcript_artifacts(paths: _TranscriptArtifactPaths, transcript: Transcript) -> None:
    """Write canonical JSON atomically BEFORE Markdown, then Markdown.

    Order: canonical JSON is the source of truth and is written first via
    the accepted :func:`write_transcript_json` writer. The Markdown is then
    rendered from the SAME validated ``Transcript`` and written via the
    accepted :func:`write_markdown` writer. If Markdown rendering or writing
    fails, the already-written canonical JSON remains available and this
    function raises (it does NOT claim Markdown succeeded).
    """

    # 1. Canonical JSON first (source of truth). Atomic write via the accepted
    #    storage writer.
    write_transcript_json(paths.transcript_json, transcript)

    # 2. Render Markdown from the validated transcript. Rendering may raise
    #    (e.g. out-of-range displayed times) BEFORE any Markdown write is
    #    attempted; in that case JSON is already on disk and intact.
    markdown_text = render_transcript_markdown(transcript)

    # 3. Write Markdown atomically via the accepted writer. A failure here
    #    leaves the canonical JSON intact; the exception propagates.
    _write_text_atomic(paths.transcript_md, markdown_text)


__all__ = [
    "anchor_for",
    "format_timestamp",
    "render_transcript_markdown",
    "write_transcript_artifacts",
]
