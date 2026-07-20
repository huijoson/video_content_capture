"""Deterministic Traditional-Chinese Markdown renderer for the canonical report.

This module is a PURE RENDERER. It:

* Takes a validated :class:`Report` (group 8's grounded, provider-neutral
  output) together with its embedded canonical :class:`Transcript`.
* Renders the six required Traditional Chinese sections with the exact
  headings ``三分鐘掌握影片``, ``核心重點``, ``重要數字與說法``,
  ``名詞白話解釋``, ``結論與可能影響``, and ``來源索引``.
* Derives ``HH:MM:SS`` timestamp labels in code from source segment IDs and
  links each evidence ID to the exact canonical transcript anchor using the
  group-7 :func:`anchor_for` helper. The model never authors timestamps
  (the ``Report`` model carries none); the renderer never trusts or accepts
  model-supplied timestamps/URLs.
* Renders the source index deterministically: deduplicated and ordered by
  canonical transcript chronology (segment start time), regardless of the
  model's source-index ordering.
* Preserves general-reader text verbatim apart from safe Markdown
  escaping/formatting. Markdown-sensitive content (``#``, leading ``>``,
  multiline text) is contained so it cannot create extra sections, headings,
  or broken links.
* Surfaces every source-dependent section's evidence visibly (anchor link +
  timestamp label).

It MUST NOT import a provider SDK, network library, the normalization module,
the prompts module, or any LLM/SDK package. It MUST NOT redefine atomic-write
helpers; it reuses the accepted :mod:`storage.artifacts` writers. It MUST NOT
call a cloud provider or LLM.

Renderer-level guards: even though the :class:`Report` model already enforces
known evidence IDs and required sections, the renderer additionally asserts
that every referenced segment's displayed timing lies within media duration
(``[0, metadata.duration_seconds]``). Unknown IDs, duplicate/missing required
sections, invalid evidence, and out-of-range referenced segments must fail
BEFORE Markdown is written.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Protocol

from video_content_capture.domain.errors import GroundingError
from video_content_capture.domain.models import Report, ReportItem
from video_content_capture.markdown.transcript import anchor_for, format_timestamp
from video_content_capture.storage.artifacts import write_markdown, write_report_json

# Document title (Traditional Chinese).
_DOCUMENT_TITLE = "影片重點摘要"

# Visible warning that the report derives from ASR transcript content.
_ASR_WARNING_LINE = (
    "> ⚠️ 本報告由自動語音辨識（ASR）逐字稿產生，內容可能包含錯誤，請以原始影音為準。"
)


# --- Protocol for artifact paths ------------------------------------------


class _ReportArtifactPaths(Protocol):
    """Minimal path surface the orchestration helper needs.

    Implemented by :class:`storage.paths.ArtifactPaths`; declared here as a
    Protocol so the renderer does not import the concrete paths dataclass.
    """

    @property
    def report_json(self) -> Path: ...

    @property
    def report_md(self) -> Path: ...


# --- Renderer-level guards ------------------------------------------------


def _validate_evidence_timing(report: Report) -> None:
    """Assert every referenced segment's timing lies within media duration.

    The :class:`Report` model already enforces that every evidence ID is a
    known canonical segment ID. This guard additionally bounds referenced
    segment timing to ``[0, duration_seconds]`` so a programmatically-
    constructed report (e.g. a reducer) cannot yield displayed times that
    exceed the source media. Raises :class:`GroundingError` on violation so
    callers classify it as a grounding failure (failing before Markdown).
    """

    duration = report.transcript.metadata.duration_seconds
    id_to_segment = {seg.segment_id: seg for seg in report.transcript.segments}
    for section in report.sections:
        for sid in section.source_segment_ids:
            seg = id_to_segment[sid]
            if seg.start < 0 or seg.end > duration:
                raise GroundingError(
                    f"section {section.section_type!r} references segment {sid!r} "
                    f"whose timing [{seg.start}, {seg.end}] is out of range of "
                    f"media duration {duration}",
                    details={
                        "section": section.section_type,
                        "segment_id": sid,
                        "start": seg.start,
                        "end": seg.end,
                        "duration": duration,
                    },
                )


# --- Text escaping --------------------------------------------------------


def _escape_inline(text: str) -> str:
    """Escape Markdown-inline characters that would break structure.

    Only structural inline characters are escaped: ``[`` and ``]`` (which
    would start/end link text) and backticks (which would start inline code).
    Pipe ``|`` is escaped so content cannot break out of a future table cell.
    Angle brackets, ``#``, ``>``, and other block-level markers are NOT
    escaped here because block-level containment is handled by rendering
    free-form text as paragraphs/quoted lines (see :func:`_render_paragraph`)
    rather than as raw Markdown lines.
    """

    return (
        text.replace("\\", "\\\\")
        .replace("[", "\\[")
        .replace("]", "\\]")
        .replace("`", "\\`")
        .replace("|", "\\|")
    )


def _render_paragraph(text: str) -> str:
    """Render free-form text so it cannot create extra headings or blocks.

    Used for the overview (a single prose paragraph) and as a fallback for
    sections without structured items. Strategy: split on newlines and emit
    each line with inline structural characters escaped; a line starting with
    ``#`` or ``>`` is additionally backslash-escaped at its leading character
    so it cannot be interpreted as a heading or blockquote.
    """

    if not text:
        return ""
    out: list[str] = []
    for line in text.splitlines() or [text]:
        escaped = _escape_inline(line)
        if escaped.startswith("#"):
            out.append("\\" + escaped)
        elif escaped.startswith(">"):
            out.append("\\" + escaped)
        else:
            out.append(escaped)
    return "\n".join(out)


# --- Evidence link rendering (inline, per item) ---------------------------


def _encode_markdown_href(value: str) -> str:
    """Percent-encode characters that break Markdown link destinations."""

    return value.replace("%", "%25").replace(" ", "%20").replace("(", "%28").replace(")", "%29")


def _default_transcript_href(report: Report) -> str:
    stem = Path(report.transcript.metadata.source_path).stem or "transcript"
    return _encode_markdown_href(f"{stem}.transcript.md")


def _evidence_links(source_segment_ids: list[str], report: Report, *, transcript_href: str) -> str:
    """Render inline evidence links for one item's source segment IDs.

    Each evidence ID becomes a Markdown link whose label is the code-derived
    ``HH:MM:SS–HH:MM:SS｜講者 X`` (timestamp + anonymous speaker) and whose
    target is the canonical transcript anchor (``#seg-<id>``) produced by the
    group-7 :func:`anchor_for` helper. Timestamps/links are derived in code
    from the canonical segment; the model never authors them.

    The links are ordered by the item's evidence ID order (the model's per-
    item ordering is preserved; only the source INDEX is reordered by
    chronology).
    """

    parts: list[str] = []
    for sid in source_segment_ids:
        seg = next(s for s in report.transcript.segments if s.segment_id == sid)
        label = f"{format_timestamp(seg.start)}–{format_timestamp(seg.end)}｜{seg.speaker_label}"
        anchor = anchor_for(sid)
        parts.append(f"[{label}]({transcript_href}#{anchor})")
    return " ".join(parts)


def _render_item(
    item: ReportItem,
    report: Report,
    *,
    is_glossary: bool,
    transcript_href: str,
) -> str:
    """Render one structured :class:`ReportItem` as a Markdown bullet.

    The item ``text`` (and ``label`` for glossary) is escaped inline and
    preserved verbatim apart from Markdown escaping. Inline evidence links
    are appended for every source segment ID on the item. When
    ``is_speaker_opinion`` is True, a visible ``（講者觀點）`` label is
    appended (the flag is authoritative — the renderer never infers opinion
    from text).
    """

    text = _escape_inline(item.text)
    body = text
    if is_glossary and item.label:
        body = f"{_escape_inline(item.label)}：{text}"
    links = _evidence_links(item.source_segment_ids, report, transcript_href=transcript_href)
    if links:
        body = f"{body} {links}"
    if item.is_speaker_opinion:
        body = f"{body}（講者觀點）"
    return f"- {body}"


# --- Source index rendering (chronological, deduplicated, linked) ---------


def _chronological_dedup(ids: list[str], report: Report) -> list[str]:
    """Return ``ids`` deduplicated and ordered by canonical segment start time.

    The model's source-index ordering is deliberately ignored: the renderer
    reorders referenced IDs by their canonical transcript chronology so the
    source index is deterministic and chronological regardless of model
    ordering.
    """

    id_to_segment = {seg.segment_id: seg for seg in report.transcript.segments}
    seen: set[str] = set()
    unique: list[str] = []
    for sid in ids:
        if sid not in seen:
            seen.add(sid)
            unique.append(sid)
    return sorted(unique, key=lambda sid: id_to_segment[sid].start)


def _render_source_index(ids: list[str], report: Report, *, transcript_href: str) -> str:
    """Render the source index as chronological, deduplicated anchor links.

    Each entry is a Markdown link whose label is the code-derived
    ``HH:MM:SS–HH:MM:SS｜講者 X`` (timestamp + anonymous speaker) and whose
    target is the canonical transcript anchor (``#seg-<id>``). The segment ID
    is shown after the link so the entry is recognizable in plain text too.
    """

    ordered = _chronological_dedup(ids, report)
    if not ordered:
        return ""
    lines: list[str] = []
    for sid in ordered:
        seg = next(s for s in report.transcript.segments if s.segment_id == sid)
        label = f"{format_timestamp(seg.start)}–{format_timestamp(seg.end)}｜{seg.speaker_label}"
        anchor = anchor_for(sid)
        lines.append(f"- [{label}]({transcript_href}#{anchor}) — {sid}")
    return "\n".join(lines)


# --- Section rendering ----------------------------------------------------


def _render_section(section_title: str, body: str) -> str:
    """Render one section as a level-2 heading followed by its body."""

    body = body.strip()
    if body:
        return f"## {section_title}\n\n{body}"
    return f"## {section_title}\n"


def _section_body(report: Report, section_type: str, *, transcript_href: str) -> str:
    """Return the body string for a section by type.

    Itemized sections (core_topics, important_numbers, glossary, conclusion)
    render from structured :class:`ReportItem` entries with inline evidence
    links and ``講者觀點`` labels. The overview renders as a prose paragraph
    with inline evidence links when the overview item carries evidence. The
    source index renders as chronological, deduplicated anchor links.
    """

    section = next(s for s in report.sections if s.section_type == section_type)
    if section_type == "source_index":
        return _render_source_index(
            section.source_segment_ids, report, transcript_href=transcript_href
        )
    if section_type == "overview":
        # Overview: a single prose item; render as a paragraph with inline
        # evidence links appended (no bullet).
        if not section.items:
            return _render_paragraph(section.content)
        item = section.items[0]
        text = _render_paragraph(item.text)
        links = _evidence_links(item.source_segment_ids, report, transcript_href=transcript_href)
        if links:
            return f"{text} {links}" if text else links
        return text
    # Itemized sections.
    is_glossary = section_type == "glossary"
    lines = [
        _render_item(
            item,
            report,
            is_glossary=is_glossary,
            transcript_href=transcript_href,
        )
        for item in section.items
    ]
    return "\n".join(lines)


def render_report_markdown(report: Report, *, transcript_href: str | None = None) -> str:
    """Render a validated :class:`Report` to a deterministic Markdown string.

    Pure: no provider/network/normalization/LLM calls. Timestamps and anchor
    links are code-derived from canonical segment IDs; the source index is
    chronological and deduplicated; ``講者觀點`` labels are projected from
    the structured ``is_speaker_opinion`` flag (never inferred from text).
    """

    _validate_evidence_timing(report)
    resolved_transcript_href = (
        _default_transcript_href(report)
        if transcript_href is None
        else _encode_markdown_href(transcript_href)
    )

    parts: list[str] = [
        f"# {_DOCUMENT_TITLE}",
        "",
        _ASR_WARNING_LINE,
        "",
    ]

    # Render sections in the canonical fixed order (independent of the order
    # the sections list happens to be in).
    titles = (
        ("overview", "三分鐘掌握影片"),
        ("core_topics", "核心重點"),
        ("important_numbers", "重要數字與說法"),
        ("glossary", "名詞白話解釋"),
        ("conclusion", "結論與可能影響"),
        ("source_index", "來源索引"),
    )
    for section_type, title in titles:
        body = _section_body(report, section_type, transcript_href=resolved_transcript_href)
        parts.append(_render_section(title, body))
        parts.append("")

    return "\n".join(parts).rstrip("\n") + "\n"


# --- Orchestration: JSON before Markdown ----------------------------------


def _write_markdown(path: Path, text: str) -> None:
    """Indirection point used by :func:`write_report_artifacts`.

    Exists ONLY so tests can monkeypatch the Markdown write to simulate a
    failure AFTER JSON is written. Delegates to the accepted
    :func:`video_content_capture.storage.artifacts.write_markdown` writer —
    it does NOT reimplement atomic writes.
    """

    write_markdown(path, text)


def write_report_artifacts(
    paths: _ReportArtifactPaths,
    report: Report,
    *,
    transcript_markdown_path: Path | None = None,
) -> None:
    """Write canonical report JSON atomically BEFORE Markdown, then Markdown.

    Order: canonical JSON is the source of truth and is written first via the
    accepted :func:`write_report_json` writer. The Markdown is then rendered
    from the SAME validated ``Report`` and written via the accepted
    :func:`write_markdown` writer. If Markdown rendering or writing fails, the
    already-written canonical JSON remains available and this function raises
    (it does NOT claim Markdown succeeded).
    """

    # 1. Canonical JSON first (source of truth). Atomic write via the accepted
    #    storage writer.
    write_report_json(paths.report_json, report)

    # 2. Render Markdown from the validated report. When the transcript lives
    #    in another directory, compute a relative cross-document target from
    #    the report Markdown location so every evidence link remains usable.
    transcript_href: str | None = None
    if transcript_markdown_path is not None:
        relative = os.path.relpath(transcript_markdown_path, start=paths.report_md.parent)
        transcript_href = Path(relative).as_posix()
    markdown_text = render_report_markdown(report, transcript_href=transcript_href)

    # 3. Write Markdown atomically via the accepted writer. A failure here
    #    leaves the canonical JSON intact; the exception propagates.
    _write_markdown(paths.report_md, markdown_text)


__all__ = ["render_report_markdown", "write_report_artifacts"]
