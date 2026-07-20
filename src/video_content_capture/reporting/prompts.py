"""Model-facing prompts for grounded Traditional Chinese report generation.

This module is PURE and provider-neutral: it builds the ordered structured
segment payload and the system/user prompt text. It imports NO provider SDK.
The prompt explicitly requires:

* Traditional Chinese for general readers (繁體中文).
* Plain-language explanations of necessary financial terms.
* No independent fact claims and no investment advice (不可提供投資建議).
* ``講者觀點`` labeling for forecasts, judgments, and recommendations.
* Source segment IDs only — the model MUST NOT author timestamps, links, or
  Markdown; every source-dependent item carries ``source_segment_ids``.

The segment payload exposes ``segment_id``, ``start_seconds`` /
``end_seconds`` (as CONTEXT for ordering only — the renderer derives
authoritative timestamps in code), ``speaker_label`` (anonymous), and
``normalized_text``. Raw text is NOT exposed by default (per the brief: raw
text only when uncertainty/evidence requires it).
"""

from __future__ import annotations

from typing import Any

from video_content_capture.domain.models import Transcript

# --- Prompt constants (Traditional Chinese) -------------------------------

_SYSTEM_PROMPT = (
    "你是一位財經內容摘要助手。你的任務是根據提供的逐字稿區段，"
    "為一般讀者撰寫繁體中文（Traditional Chinese）的報告。\n"
    "\n"
    "嚴格規則：\n"
    "1. 全部輸出必須使用繁體中文，並以一般讀者能理解的語言撰寫；必要時"
    "用白話解釋財經名詞，但不可改變原始數字、幣別、機構名稱或股票代碼的意義。\n"
    "2. 不可做出獨立於講者內容的事實宣稱，不可提供投資建議"
    "（不可建議買進、賣出或持有任何標的）。\n"
    "3. 當內容涉及講者的預測、判斷或建議時，必須將該項目標記為"
    "「講者觀點」，不得呈現為已驗證的事實。\n"
    "4. 每一個依賴來源的項目都必須在 ``source_segment_ids`` 中提供一或多個"
    "來源 segment ID（區段 ID）。只能使用提供的 segment ID，不得發明新的 ID。\n"
    "5. 不得輸出時間戳記、連結或 Markdown；時間戳記與連結由系統依據"
    "segment ID 自行推導，模型絕不提供權威時間。\n"
    "6. 確實產出六個必要章節：overview（三分鐘掌握影片）、"
    "core_topics（核心重點）、important_numbers（重要數字與說法）、"
    "glossary（名詞白話解釋）、conclusion（結論與可能影響）、"
    "source_index（來源索引）。\n"
    "\n"
    "章節結構說明：\n"
    "- overview：三分鐘掌握影片，整體摘要，供一般讀者快速理解。\n"
    "- core_topics：核心重點列表，每項必須帶 source_segment_ids；"
    "若為講者預測/判斷/建議，is_speaker_opinion 設為 true。\n"
    "- important_numbers：重要數字與說法列表，每項必須帶 source_segment_ids；"
    "若為講者觀點，is_speaker_opinion 設為 true。\n"
    "- glossary：名詞白話解釋列表，解釋必要財經名詞；可選擇帶 source_segment_ids。\n"
    "- conclusion：結論與可能影響，必須帶 source_segment_ids；"
    "若為講者預測/判斷/建議，is_speaker_opinion 必須為 true。\n"
    "- source_index：列出本報告引用的所有 segment ID（entries）。\n"
)


_USER_PROMPT_PREAMBLE = (
    "以下是依照時間順序排列的逐字稿區段。每個區段包含 segment ID、"
    "起訖秒數（僅供排序與定位參考，不得作為權威時間輸出）、"
    "匿名講者標籤（如「講者 A」）與繁體中文正文。\n"
    "\n"
    "請依上述規則產出結構化報告，所有依賴來源的項目都必須帶有效的 segment ID。\n"
    "\n"
    "逐字稿區段（JSON）：\n"
)


# --- Segment payload ------------------------------------------------------


def build_segment_payload(
    transcript: Transcript,
    *,
    segments: list[Any] | None = None,
) -> list[dict[str, Any]]:
    """Build the ordered structured segment payload for the model.

    Segments are sent in canonical (start-time) order. Each payload entry
    carries:

    * ``segment_id`` — the stable canonical ID the model references as evidence.
    * ``start_seconds`` / ``end_seconds`` — timing as CONTEXT for ordering
      and定位 only. The renderer derives authoritative timestamps from these
      IDs in code; the model MUST NOT output timestamps.
    * ``speaker_label`` — anonymous deterministic Traditional Chinese label.
    * ``normalized_text`` — the normalized Traditional Chinese text.

    Raw text is NOT included by default (per the brief: raw text only when
    uncertainty/evidence requires it). A future caller may extend the payload
    to include raw text for specific segments; that path is not used here.

    ``segments`` may restrict the payload to a subset of the transcript's
    segments (used by the map step for bounded chronological groups). When
    omitted, the full transcript is used.
    """

    if segments is None:
        segments = transcript.segments
    payload: list[dict[str, Any]] = []
    for seg in segments:
        # Accept either a TranscriptSegment (attribute access) or a pre-built
        # payload dict (passthrough). The passthrough path is used by the
        # adapter's reduce step, which synthesizes a sentinel "segment" dict
        # to ask the model to merge map results.
        if isinstance(seg, dict):
            payload.append(dict(seg))
            continue
        payload.append(
            {
                "segment_id": seg.segment_id,
                "start_seconds": seg.start,
                "end_seconds": seg.end,
                "speaker_label": seg.speaker_label,
                "normalized_text": seg.normalized_text,
            }
        )
    return payload


# --- Prompt assembly ------------------------------------------------------


def build_reporting_prompt(
    transcript: Transcript,
    *,
    segments: list[Any] | None = None,
) -> tuple[str, str]:
    """Build the (system, user) prompt pair for one reporting call.

    The user message embeds the ordered structured segment payload as JSON so
    the model receives deterministic, evidence-addressable segments. No
    credentials are ever embedded in the prompt.
    """

    import json

    payload = build_segment_payload(transcript, segments=segments)
    user = _USER_PROMPT_PREAMBLE + json.dumps(payload, ensure_ascii=False)
    return _SYSTEM_PROMPT, user


__all__ = ["build_reporting_prompt", "build_segment_payload"]
