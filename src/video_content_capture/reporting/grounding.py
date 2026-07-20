"""Grounding validation for model-returned structured reports.

Grounding runs BEFORE any report renderer or storage write. It validates a
model-returned structured payload (the :class:`StructuredReport` shape) against
the canonical :class:`Transcript` and produces a provider-neutral validated
:class:`Report`. It rejects:

* Unknown segment IDs (not in the canonical transcript).
* Empty evidence for source-dependent claims.
* Malformed / missing / duplicate required sections.
* Out-of-range referenced segment timing (a referenced segment's timing must
  lie within the canonical media duration ``[0, duration_seconds]``).
* Missing ``講者觀點`` viewpoint labeling where the structured item requires
  it (a conclusion that reads as a forecast/judgment/recommendation must
  carry ``is_speaker_opinion=True``).

The resulting :class:`Report` carries source segment IDs only — NO
model-authored timestamps. Group 9's renderer derives all timestamps and
links from the canonical transcript segments by code.

This module is provider-neutral: it imports NO provider SDK.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from video_content_capture.domain.errors import GroundingError
from video_content_capture.domain.models import (
    REQUIRED_REPORT_SECTION_TYPES,
    Report,
    ReportItem,
    ReportSection,
    Transcript,
)
from video_content_capture.reporting.base import (
    SOURCE_DEPENDENT_SECTION_TYPES,
    StructuredReport,
)

# Traditional Chinese section titles (canonical rendering order).
_SECTION_TITLES: dict[str, str] = {
    "overview": "三分鐘掌握影片",
    "core_topics": "核心重點",
    "important_numbers": "重要數字與說法",
    "glossary": "名詞白話解釋",
    "conclusion": "結論與可能影響",
    "source_index": "來源索引",
}

# Keywords that signal a forecast/judgment/recommendation in the conclusion
# text. If any appears and ``is_speaker_opinion`` is False, grounding rejects
# the response (the renderer would otherwise present a viewpoint as fact).
_OPINION_KEYWORDS: tuple[str, ...] = (
    "預期",
    "預測",
    "我認為",
    "我覺得",
    "可能會",
    "應該會",
    "估計",
    "預料",
    "建議",
    "推測",
)


# --- Public API -----------------------------------------------------------


def ground_structured_report(
    transcript: Transcript,
    structured: dict[str, Any] | StructuredReport,
) -> Report:
    """Validate a model-returned structured payload and return a provider-neutral Report.

    Raises :class:`GroundingError` for any rejected shape (unknown IDs, empty
    evidence, missing/duplicate/malformed sections, out-of-range timing,
    missing opinion labeling).
    """

    model = _coerce_structured(structured)
    valid_ids = transcript.segment_ids
    duration = transcript.metadata.duration_seconds
    id_to_segment = {seg.segment_id: seg for seg in transcript.segments}

    _validate_evidence_ids(model, valid_ids, id_to_segment, duration)
    _validate_opinion_labeling(model)

    sections = _build_sections(transcript, model, id_to_segment)
    # Build the provider-neutral Report; the Report model itself also enforces
    # unknown-ID and required-section invariants, so any slip becomes a
    # GroundingError here rather than surfacing downstream.
    try:
        return Report(transcript=transcript, sections=sections)
    except ValidationError as exc:
        raise GroundingError(
            "grounding produced an invalid report",
            details={"errors": _errors_from_validation(exc)},
        ) from exc
    except ValueError as exc:
        raise GroundingError(str(exc), details={}) from exc


# --- Internal: parse/coerce ----------------------------------------------


def _coerce_structured(structured: dict[str, Any] | StructuredReport) -> StructuredReport:
    if isinstance(structured, StructuredReport):
        return structured
    try:
        return StructuredReport.model_validate(structured)
    except ValidationError as exc:
        raise GroundingError(
            "malformed structured report: required sections missing/duplicated or invalid shapes",
            details={"errors": _errors_from_validation(exc)},
        ) from exc


def _errors_from_validation(exc: ValidationError) -> list[dict[str, Any]]:
    return [
        {"loc": list(err["loc"]), "msg": err["msg"], "type": err["type"]} for err in exc.errors()
    ]


# --- Internal: evidence ID + timing validation ---------------------------


def _validate_evidence_ids(
    model: StructuredReport,
    valid_ids: set[str],
    id_to_segment: dict[str, Any],
    duration: float,
) -> None:
    """Reject unknown IDs, empty source-dependent evidence, and out-of-range timing."""

    # Collect every referenced ID grouped by section.
    referenced: list[tuple[str, str]] = []  # (section_type, segment_id)

    for sid in model.overview.source_segment_ids:
        referenced.append(("overview", sid))
    for topic in model.core_topics:
        if not topic.source_segment_ids:
            raise GroundingError(
                "core_topics item has empty evidence for a source-dependent claim",
                details={},
            )
        for sid in topic.source_segment_ids:
            referenced.append(("core_topics", sid))
    for number in model.important_numbers:
        if not number.source_segment_ids:
            raise GroundingError(
                "important_numbers item has empty evidence for a source-dependent claim",
                details={},
            )
        for sid in number.source_segment_ids:
            referenced.append(("important_numbers", sid))
    for entry in model.glossary:
        for sid in entry.source_segment_ids:
            referenced.append(("glossary", sid))
    if not model.conclusion.source_segment_ids:
        raise GroundingError(
            "conclusion has empty evidence for a source-dependent claim",
            details={},
        )
    for sid in model.conclusion.source_segment_ids:
        referenced.append(("conclusion", sid))
    for sid in model.source_index.entries:
        referenced.append(("source_index", sid))

    # Validate every referenced ID is known AND in-range.
    for section_type, sid in referenced:
        if sid not in valid_ids:
            raise GroundingError(
                f"section {section_type!r} references unknown source segment ID {sid!r}",
                details={"section": section_type, "segment_id": sid},
            )
        seg = id_to_segment[sid]
        if seg.start < 0 or seg.end > duration:
            raise GroundingError(
                f"section {section_type!r} references segment {sid!r} whose timing "
                f"[{seg.start}, {seg.end}] is out of range of media duration {duration}",
                details={
                    "section": section_type,
                    "segment_id": sid,
                    "start": seg.start,
                    "end": seg.end,
                    "duration": duration,
                },
            )


# --- Internal: opinion labeling ------------------------------------------


def _validate_opinion_labeling(model: StructuredReport) -> None:
    """Require ``is_speaker_opinion=True`` for source-dependent items that read
    as a forecast/judgment/recommendation.

    Applied to ``core_topics``, ``important_numbers``, and ``conclusion``. A
    conservative Traditional-Chinese keyword guard detects viewpoint-bearing
    text (預期/預測/我認為/我覺得/可能會/應該會/估計/預料/建議/推測). When any
    keyword appears and ``is_speaker_opinion`` is False, grounding rejects so
    the renderer (group 9) can label the item as 講者觀點 rather than presenting
    a speaker viewpoint as independently verified fact.

    The guard is intentionally conservative: it only triggers on explicit
    forecast/judgment/recommendation keywords, so non-viewpoint factual
    reporting is accepted without the flag. The model is also instructed via
    the prompt to set the flag for forecasts; this guard is the enforcement
    seam that catches a model that fails to do so.
    """

    _check_opinion("core_topics", model.core_topics, text_attr="topic")
    _check_opinion("important_numbers", model.important_numbers, text_attr="number_or_claim")
    _check_opinion(
        "conclusion",
        [model.conclusion],
        text_attr="conclusion",
    )
    _check_opinion(
        "conclusion",
        [model.conclusion],
        text_attr="possible_impact",
    )


def _check_opinion(section_type: str, items: Any, *, text_attr: str) -> None:
    """Reject items whose ``text_attr`` text reads as a viewpoint but whose
    ``is_speaker_opinion`` flag is False."""

    for item in items:
        if item.is_speaker_opinion:
            continue
        text = getattr(item, text_attr, "")
        if any(kw in text for kw in _OPINION_KEYWORDS):
            raise GroundingError(
                f"{section_type} item reads as a speaker viewpoint "
                f"(forecast/judgment/recommendation) but is_speaker_opinion is "
                f"False; forecasts must be labeled 講者觀點",
                details={"section": section_type, "text": text},
            )


# --- Internal: build provider-neutral sections ---------------------------


def _build_sections(
    transcript: Transcript,
    model: StructuredReport,
    id_to_segment: dict[str, Any],
) -> list[ReportSection]:
    """Build the provider-neutral :class:`ReportSection` list in canonical order.

    Each section carries structured :class:`ReportItem` entries (verbatim
    text, per-item evidence IDs, opinion flag) and a section-level
    ``source_segment_ids`` that is the deterministic, deduplicated,
    order-preserving union of item evidence IDs. Canonical ``content``
    carries NO Markdown bullets, NO ``（講者觀點）`` label, and NO presentation
    prefixes — presentation is owned by the group-9 renderer. Group 9 derives
    timestamps/links from these IDs in code.
    """

    # overview (not source-dependent; per-item evidence optional)
    overview_ids = list(model.overview.source_segment_ids)
    overview_items = [
        ReportItem(
            text=model.overview.summary,
            source_segment_ids=overview_ids,
        )
    ]
    sections: list[ReportSection] = [
        ReportSection(
            section_type="overview",
            title=_SECTION_TITLES["overview"],
            content=model.overview.summary,
            items=overview_items,
            source_segment_ids=overview_ids,
            is_source_dependent=False,
        )
    ]

    # core_topics
    core_items = [
        ReportItem(
            text=item.topic,
            source_segment_ids=list(item.source_segment_ids),
            is_speaker_opinion=item.is_speaker_opinion,
        )
        for item in model.core_topics
    ]
    core_ids = _dedupe_preserve_order(
        sid for item in model.core_topics for sid in item.source_segment_ids
    )
    sections.append(
        ReportSection(
            section_type="core_topics",
            title=_SECTION_TITLES["core_topics"],
            content=_plain_verbatim_join([item.text for item in core_items]),
            items=core_items,
            source_segment_ids=core_ids,
            is_source_dependent=True,
        )
    )

    # important_numbers
    number_items = [
        ReportItem(
            text=item.number_or_claim,
            source_segment_ids=list(item.source_segment_ids),
            is_speaker_opinion=item.is_speaker_opinion,
        )
        for item in model.important_numbers
    ]
    num_ids = _dedupe_preserve_order(
        sid for item in model.important_numbers for sid in item.source_segment_ids
    )
    sections.append(
        ReportSection(
            section_type="important_numbers",
            title=_SECTION_TITLES["important_numbers"],
            content=_plain_verbatim_join([item.text for item in number_items]),
            items=number_items,
            source_segment_ids=num_ids,
            is_source_dependent=True,
        )
    )

    # glossary (not source-dependent; evidence optional)
    glossary_items = [
        ReportItem(
            label=item.term,
            text=item.explanation,
            source_segment_ids=list(item.source_segment_ids),
        )
        for item in model.glossary
    ]
    gloss_ids = _dedupe_preserve_order(
        sid for item in model.glossary for sid in item.source_segment_ids
    )
    sections.append(
        ReportSection(
            section_type="glossary",
            title=_SECTION_TITLES["glossary"],
            content=_plain_verbatim_join([item.text for item in glossary_items]),
            items=glossary_items,
            source_segment_ids=gloss_ids,
            is_source_dependent=False,
        )
    )

    # conclusion: the model's conclusion + possible_impact are projected as
    # TWO structured items so the renderer can attach evidence links to each
    # independently. The ``possible_impact`` is part of the conclusion section
    # (same evidence set); it is NOT a forecast label.
    conclusion_items = [
        ReportItem(
            text=model.conclusion.conclusion,
            source_segment_ids=list(model.conclusion.source_segment_ids),
            is_speaker_opinion=model.conclusion.is_speaker_opinion,
        )
    ]
    if model.conclusion.possible_impact:
        conclusion_items.append(
            ReportItem(
                text=model.conclusion.possible_impact,
                source_segment_ids=list(model.conclusion.source_segment_ids),
                is_speaker_opinion=model.conclusion.is_speaker_opinion,
            )
        )
    sections.append(
        ReportSection(
            section_type="conclusion",
            title=_SECTION_TITLES["conclusion"],
            content=model.conclusion.conclusion,
            items=conclusion_items,
            source_segment_ids=list(model.conclusion.source_segment_ids),
            is_source_dependent=True,
        )
    )

    # source_index: each referenced ID is an item carrying the ID as its text
    # and evidence; the renderer reorders chronologically and renders links.
    index_ids = _dedupe_preserve_order(model.source_index.entries)
    index_items = [ReportItem(text=sid, source_segment_ids=[sid]) for sid in index_ids]
    sections.append(
        ReportSection(
            section_type="source_index",
            title=_SECTION_TITLES["source_index"],
            content="",
            items=index_items,
            source_segment_ids=index_ids,
            is_source_dependent=False,
        )
    )

    # Sanity: all required types present exactly once (the Report model also
    # enforces this, but we fail early with a GroundingError for clarity).
    present = [s.section_type for s in sections]
    for required in REQUIRED_REPORT_SECTION_TYPES:
        if present.count(required) != 1:
            raise GroundingError(
                f"required section {required!r} must appear exactly once",
                details={"present": present},
            )
    _ = SOURCE_DEPENDENT_SECTION_TYPES  # documented contract reference
    _ = transcript  # transcript is used by the Report model, not here directly
    _ = id_to_segment  # reserved for future renderer-internal use
    return sections


def _plain_verbatim_join(texts: list[str]) -> str:
    """Join item texts with newlines, verbatim — no Markdown bullets, no
    labels, no prefixes. Canonical ``content`` is structured data, not
    presentation."""

    return "\n".join(t for t in texts if t)


def _dedupe_preserve_order(ids: Any) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for sid in ids:
        if sid not in seen:
            seen.add(sid)
            out.append(sid)
    return out


__all__ = ["ground_structured_report"]
