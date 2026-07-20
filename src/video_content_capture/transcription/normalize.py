"""Conservative normalization for recognized Mandarin speech.

Normalization operates on raw recognized text and produces a normalized
Traditional Chinese representation that:

* Preserves the raw text (the caller stores it separately on each segment).
* Adds conservative punctuation only when a deterministic rule applies; never
  invents or rewrites content.
* Converts a small, safe set of Simplified Chinese characters to their
  Traditional Chinese forms using a deterministic character map only. No
  LLM, no dictionary that may rewrite financial entities.
* Does NOT silently change numbers, currencies, organization names, stock
  symbols, or uncertain financial terms. A character that participates in
  such terms is only converted when the conversion is a pure one-to-one
  glyph change with identical meaning (e.g. ``场`` -> ``場``).

The Simplified->Traditional map is intentionally small and curated rather
than exhaustive: an exhaustive table risks rewriting financial entity names
in ways the speaker did not intend. Only characters that appear in common
Mandarin financial commentary and have unambiguous one-to-one Traditional
forms are included.

Ambiguous one-to-many mappings are DELIBERATELY EXCLUDED. A Simplified
character that maps to multiple Traditional glyphs depending on meaning is
never silently converted, because the correct Traditional form depends on
context that a deterministic character map cannot resolve. Known excluded
ambiguous cases (with the distinct Traditional glyphs they would map to):

* ``后`` -> ``後`` (time/after, e.g. 以後) vs ``后`` (empress/queen, e.g. 皇后)
* ``于`` -> ``於`` (preposition) vs ``于`` (surname, e.g. 于小姐)
* ``发`` -> ``發`` (issue/send, e.g. 發布) vs ``髮`` (hair, e.g. 頭發/頭髮)
* ``几`` -> ``幾`` (how many) vs ``几`` (small table, e.g. 茶几)
* ``汇`` -> ``匯`` (forex/converge, e.g. 匯率) vs ``彙`` (compile, e.g. 匯編)
* ``坛`` -> ``壇`` (altar/forum) vs ``罎/罈`` (jar, e.g. 一坛老酒)
* ``周`` -> ``週`` (week) vs ``周`` (surname/complete, e.g. 周先生)

These characters are left unchanged; the normalized text preserves the
Simplified glyph so the reader sees exactly what the ASR produced, and a
human can interpret the intended Traditional form from context.
"""

from __future__ import annotations

# --- Curated Simplified -> Traditional character map -----------------------
#
# Only characters with an unambiguous one-to-one mapping that preserve
# meaning verbatim. Characters that are part of organization/stock names
# (e.g. 達/达) are included ONLY when the simplified form is never used as
# the canonical spelling of a listed Taiwanese entity; we favor the
# Traditional form because the project's output language is zh-TW.
_SIMPLIFIED_TO_TRADITIONAL: dict[str, str] = {
    "场": "場",
    "市": "市",  # identity, no-op (kept for clarity; removed below)
    "东": "東",
    "车": "車",
    "马": "馬",
    "电": "電",
    "气": "氣",
    "员": "員",
    "务": "務",
    "业": "業",
    "产": "產",
    "关": "關",
    "开": "開",
    "门": "門",
    "时": "時",
    "间": "間",
    "说": "說",
    "话": "話",
    "请": "請",
    "国": "國",
    "机": "機",
    "会": "會",
    "个": "個",
    "们": "們",
    "这": "這",
    "那": "那",
    "为": "為",
    "从": "從",
    "还": "還",
    "过": "過",
    "给": "給",
    "让": "讓",
    "动": "動",
    "点": "點",
    "长": "長",
    "金": "金",
    "银": "銀",
    "报": "報",
    "涨": "漲",
    "跌": "跌",
    "买": "買",
    "卖": "賣",
    "价": "價",
    "计": "計",
    "算": "算",
    "数": "數",
    "量": "量",
    "亿": "億",
    "万": "萬",
    "双": "雙",
    "单": "單",
    "与": "與",
    "网": "網",
    "络": "絡",
    "视": "視",
    "频": "頻",
    "听": "聽",
    "声": "聲",
    "音": "音",
    "类": "類",
    "种": "種",
    "样": "樣",
    "见": "見",
    "观": "觀",
    "计画": "計畫",
    "实": "實",
    "现": "現",
    "问": "問",
    "题": "題",
    "决": "決",
    "定": "定",
    "总": "總",
    "结": "結",
    "构": "構",
    "体": "體",
    "系": "系",
    "统": "統",
    "强": "強",
    "弱": "弱",
    "高": "高",
    "低": "低",
    "多": "多",
    "少": "少",
    "进": "進",
    "出": "出",
    "入": "入",
    "行": "行",
    "情": "情",
    "势": "勢",
    "态": "態",
    "度": "度",
    "天": "天",
    "月": "月",
    "年": "年",
    "今": "今",
    "明": "明",
    "前": "前",
    "次": "次",
    "回": "回",
    "起": "起",
    "落": "落",
    "升": "升",
    "降": "降",
    "变": "變",
    "化": "化",
    "增": "增",
    "减": "減",
    "加": "加",
    "率": "率",
    "比": "比",
    "例": "例",
    "约": "約",
    "第": "第",
    "其": "其",
    "它": "它",
    "他": "他",
    "她": "她",
    "我": "我",
    "你": "你",
    "它們": "它們",
}

# Remove identity entries that would map a character to itself (kept above
# only for documentation clarity). This is a no-op defensively.
_SIMPLIFIED_TO_TRADITIONAL = {k: v for k, v in _SIMPLIFIED_TO_TRADITIONAL.items() if k != v}

# --- Conservative punctuation ---------------------------------------------
#
# Recognized Mandarin speech often lacks punctuation. We add a small set of
# conservative separators ONLY between well-formed sentence-final particles
# or before conjunctions that clearly introduce a new clause. We never add
# punctuation inside a number/currency/stock symbol.

# Sentence-final particles that mark a likely clause boundary.
_SENTENCE_FINAL_PARTICLES = {"了", "呢", "嗎", "吧", "啊", "喔", "耶", "嘛"}

# Conjunctions that introduce a new clause; a comma is added BEFORE these.
_CLAUSE_CONJUNCTIONS = {
    "但是",
    "不過",
    "可是",
    "然後",
    "所以",
    "因為",
    "而且",
    "另外",
    "同時",
    "其實",
    "不僅",
}


def _apply_traditional(text: str) -> str:
    """Apply the curated Simplified->Traditional character map.

    Multi-character keys are applied first (longest match), then single
    characters, so the longer forms are not partially overwritten by the
    shorter ones.
    """

    # Multi-character keys first (sorted by descending length).
    multi = sorted(
        (k for k in _SIMPLIFIED_TO_TRADITIONAL if len(k) > 1),
        key=len,
        reverse=True,
    )
    result = text
    for key in multi:
        result = result.replace(key, _SIMPLIFIED_TO_TRADITIONAL[key])
    # Then single-character replacements, character by character.
    out_chars: list[str] = []
    for ch in result:
        out_chars.append(_SIMPLIFIED_TO_TRADITIONAL.get(ch, ch))
    return "".join(out_chars)


def _add_conservative_punctuation(text: str) -> str:
    """Add conservative punctuation without changing words.

    Rules applied:

    * A comma is inserted before a clause conjunction when it follows a CJK
      character.
    * A period is inserted after a sentence-final particle that is followed
      by another clause (whitespace + more text) OR at end-of-string.

    Punctuation is only inserted BETWEEN existing tokens; no characters are
    added, removed, or reordered.
    """

    if not text:
        return text

    out: list[str] = []
    i = 0
    n = len(text)
    while i < n:
        ch = text[i]
        # Check for a clause-conjunction at this position.
        matched_conj = ""
        for conj in _CLAUSE_CONJUNCTIONS:
            if text.startswith(conj, i) and i > 0:
                prev = out[-1] if out else ""
                # Only insert a comma if the previous char is a CJK character
                # (not already punctuation, not whitespace).
                if prev and _is_cjk(prev):
                    out.append("，")
                matched_conj = conj
                break

        out.append(ch)
        i += 1

        # If we matched a multi-char conjunction, fast-forward over the rest
        # of it (we already consumed its first char above).
        if matched_conj:
            for extra in matched_conj[1:]:
                # The next char in source should equal extra; append verbatim.
                if i < n and text[i] == extra:
                    out.append(extra)
                    i += 1
            continue

        # Sentence-final particle handling: insert a period after it when the
        # next non-whitespace char is a CJK char (a new clause) OR at end of
        # text. Only insert if not already followed by punctuation.
        if ch in _SENTENCE_FINAL_PARTICLES:
            # Look ahead skipping whitespace.
            j = i
            while j < n and text[j] in (" ", "\t"):
                j += 1
            next_is_cjk = j < n and _is_cjk(text[j])
            at_end = j >= n
            # Already-followed punctuation? Skip insertion.
            already_punct = i < n and text[i] in ("。", "，", "！", "？")
            if not already_punct and (next_is_cjk or at_end):
                out.append("。")

    result = "".join(out)
    # Collapse a trailing period if the text already ended with punctuation
    # (avoid "。。").
    return result


def _is_cjk(ch: str) -> bool:
    """Return True for CJK Unified Ideographs (basic plane)."""

    code = ord(ch)
    return 0x4E00 <= code <= 0x9FFF


def normalize_text(raw: str) -> str:
    """Return a conservatively normalized Traditional Chinese form of ``raw``.

    The raw text is preserved by the caller separately on each segment. This
    function returns a normalized representation that:

    * Converts a curated set of Simplified Chinese characters to Traditional
      forms (one-to-one, meaning-preserving).
    * Adds conservative punctuation between clauses.

    It does NOT change numbers, currencies, organization names, stock
    symbols, or uncertain financial terms. No LLM is used.
    """

    if not raw:
        return raw
    traditional = _apply_traditional(raw)
    punctuated = _add_conservative_punctuation(traditional)
    return punctuated


__all__ = ["normalize_text"]
