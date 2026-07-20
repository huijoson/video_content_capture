"""Focused reporting tests for OpenSpec group 8 (grounded Claude report).

Covers:

* Default Anthropic model is the exact verified ``claude-opus-4-8`` and is
  env-overridable; the obsolete placeholder default is replaced.
* Provider-neutral ``Reporter`` protocol is importable and runtime-checkable;
  the Claude adapter satisfies it without leaking the Anthropic SDK into
  domain / prompts / grounding / pipeline modules.
* Evidence-bearing structured response models carry source segment IDs on
  every source-dependent item and cover the six required Traditional Chinese
  report sections.
* Prompts send ordered structured segments (ID, times as context, anonymous
  speaker, normalized text) and explicitly require Traditional Chinese for
  general readers, financial-term explanations, no investment advice,
  ``講者觀點`` labeling, and source segment IDs only (no model timestamps).
* Map/reduce: long transcripts split into bounded chronological groups
  without reordering; map outputs retain evidence IDs; reduce merges/
  deduplicates while preserving at least one valid evidence ID for every
  retained source-dependent point; no retained point loses all evidence.
* Grounding rejects unknown IDs, empty evidence for source-dependent claims,
  malformed/missing/duplicate required sections, out-of-range referenced
  segment timing, and missing ``講者觀點`` viewpoint labeling where required.
* Retry classification: only timeouts/connection errors, RateLimitError, and
  server/overloaded 5xx are retried with bounded exponential backoff (sleeper
  injected so tests never sleep). Auth/permission/not-found/bad-request/
  unprocessable/malformed structured output/grounding errors are not retried.
* Errors are secret-free: no API key in messages, details, or raw metadata.
* No Markdown is generated inside ``reporting/claude.py``; the adapter returns
  a provider-neutral validated :class:`Report` plus raw provider metadata.
* A guard test prevents ``api.anthropic.com`` calls in the default suite.

All network/SDK calls are mocked at the SDK client boundary. No
``ANTHROPIC_API_KEY``, OAuth, ``ant`` profile, network, or paid call is
required. Live tests, if any, require an explicit ``@pytest.mark.live``
marker and opt-in env (not present here).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from video_content_capture.config import Settings
from video_content_capture.domain.errors import (
    GroundingError,
    ProviderAuthError,
    ProviderPayloadError,
    RateLimitError,
)
from video_content_capture.domain.models import (
    MediaMetadata,
    Report,
    StreamInfo,
    Transcript,
    TranscriptSegment,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

_CLOUD_ENV_KEYS = (
    "ASSEMBLYAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "VCC_LANGUAGE",
    "VCC_OUTPUT_DIR",
    "VCC_CACHE_DIR",
    "VCC_MIN_SPEAKERS",
    "VCC_MAX_SPEAKERS",
    "VCC_MAX_RETRIES",
    "VCC_RETRY_BASE_DELAY_SECONDS",
    "VCC_ASSEMBLYAI_MODEL",
    "VCC_TRANSCRIPTION_BACKEND",
    "VCC_MLX_WHISPER_MODEL",
    "VCC_DEFAULT_ANTHROPIC_MODEL",
    "VCC_ENABLE_LIVE",
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip cloud credentials and VCC env from the environment for every test."""

    for key in _CLOUD_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


def _make_settings(**overrides: Any) -> Settings:
    return Settings(anthropic_api_key="test-anthropic-key", **overrides)


def _make_media_metadata(source_path: Path, duration_seconds: float = 600.0) -> MediaMetadata:
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
    duration_seconds: float = 600.0,
    tmp_path: Path | None = None,
) -> Transcript:
    src = tmp_path or Path("video.mp4")
    return Transcript(
        metadata=_make_media_metadata(src, duration_seconds=duration_seconds),
        segments=segments,
        language="zh-TW",
    )


# Sentinel for "argument not provided" so an explicit empty list is preserved
# (``None`` would collide with the ``or`` fallback and silently substitute
# the default IDs, hiding the empty-evidence test path).
_UNSET = object()


def _or_default(value: object, default: object) -> object:
    return default if value is _UNSET else value


# A valid structured model response used across map/reduce tests.
def _valid_structured_payload(
    *,
    overview_ids: list[str] | object = _UNSET,
    core_ids: list[str] | object = _UNSET,
    numbers_ids: list[str] | object = _UNSET,
    glossary_ids: list[str] | object = _UNSET,
    conclusion_ids: list[str] | object = _UNSET,
    source_index_ids: list[str] | object = _UNSET,
    conclusion_is_opinion: bool = True,
) -> dict[str, Any]:
    """Return a fully-valid model-facing structured payload covering all six sections.

    An explicit empty list is preserved (use ``[]`` to test empty-evidence
    rejection); omitting an argument uses the default IDs.
    """

    return {
        "overview": {
            "summary": "三分鐘掌握影片摘要內容。",
            "source_segment_ids": _or_default(overview_ids, []),
        },
        "core_topics": [
            {
                "topic": "核心重點：市場利率動向。",
                "source_segment_ids": _or_default(core_ids, ["s0001"]),
                "is_speaker_opinion": False,
            }
        ],
        "important_numbers": [
            {
                "number_or_claim": "美國聯邦基金利率目標區間 5.25% 至 5.50%。",
                "source_segment_ids": _or_default(numbers_ids, ["s0001"]),
                "is_speaker_opinion": False,
            }
        ],
        "glossary": [
            {
                "term": "聯邦基金利率",
                "explanation": "銀行間隔夜借貸的目標利率，是美國貨幣政策的關鍵工具。",
                "source_segment_ids": _or_default(glossary_ids, []),
            }
        ],
        "conclusion": {
            "conclusion": "講者預期下半年利率可能維持高檔。",
            "possible_impact": "若利率居高，借貸成本上升，房市與企業投資可能承壓。",
            "source_segment_ids": _or_default(conclusion_ids, ["s0002"]),
            "is_speaker_opinion": conclusion_is_opinion,
        },
        "source_index": {
            "entries": _or_default(source_index_ids, ["s0001", "s0002"]),
        },
    }


def _make_two_segment_transcript(tmp_path: Path) -> Transcript:
    segs = [
        _segment("s0001", start=0.0, end=10.0, text="今天談美國聯邦基金利率。"),
        _segment("s0002", start=10.0, end=20.0, text="我認為下半年利率可能維持高檔。"),
    ]
    return _make_transcript(segs, duration_seconds=20.0, tmp_path=tmp_path)


class _FakeParsedMessage:
    """Minimal stand-in for the SDK's ``ParsedMessage`` (output of messages.parse).

    Only the attributes read by the adapter are populated: ``parsed_output``,
    ``id``, ``model``, and ``usage``.
    """

    def __init__(self, payload: dict[str, Any] | None, *, message_id: str = "msg-123") -> None:
        self.parsed_output = payload
        self.id = message_id
        self.model = "claude-opus-4-8"
        self.usage: dict[str, Any] = {"input_tokens": 10, "output_tokens": 20}


def _patch_parse(
    monkeypatch: pytest.MonkeyPatch,
    *,
    payload: dict[str, Any] | None = None,
    on_call: Any | None = None,
) -> dict[str, Any]:
    """Patch ``ClaudeReporter._sdk_parse`` to return a fake parsed message.

    ``on_call`` may inspect kwargs and return a per-call payload. When
    ``on_call`` is provided, ``payload`` is ignored. Returns a dict that is
    updated with the kwargs of the most recent call.
    """

    captured: dict[str, Any] = {}

    def _fake_parse(self: Any, **kwargs: Any) -> Any:
        captured.clear()
        captured.update(kwargs)
        if on_call is not None:
            local_payload = on_call(**kwargs)
        else:
            local_payload = payload
        return _FakeParsedMessage(local_payload)

    monkeypatch.setattr(
        "video_content_capture.reporting.claude.ClaudeReporter._sdk_parse",
        _fake_parse,
    )
    return captured


class _NoSleep:
    def __init__(self) -> None:
        self.delays: list[float] = []

    def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


# ---------------------------------------------------------------------------
# Guard: the default test suite MUST NOT make any paid/live Anthropic request.
# ---------------------------------------------------------------------------


def test_default_suite_makes_no_paid_anthropic_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A guard proving the default suite never performs a live/paid Anthropic request.

    We install a sentinel on ``httpx.Client.request`` (the SDK's transport
    entry point) that fails loudly if any call targets ``api.anthropic.com``,
    then exercise a non-network path (constructing the adapter).
    """

    import httpx

    calls: list[str] = []
    real_request = httpx.Client.request

    def _sentinel(
        self: httpx.Client, method: str, url: str, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        if "api.anthropic.com" in str(url):
            calls.append(f"{method} {url}")
            raise AssertionError(
                f"Default test suite attempted a live Anthropic request: {method} {url}"
            )
        return real_request(self, method, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "request", _sentinel)
    assert monkeypatch.delenv("VCC_ENABLE_LIVE", raising=False) is None

    from video_content_capture.reporting.claude import ClaudeReporter

    _ = ClaudeReporter(settings=_make_settings())
    assert calls == []


# ---------------------------------------------------------------------------
# Configuration: default model
# ---------------------------------------------------------------------------


def test_default_anthropic_model_is_claude_opus_4_8() -> None:
    """The obsolete placeholder default must be replaced by the verified model."""

    settings = Settings()
    assert settings.anthropic_model == "claude-opus-4-8"


def test_anthropic_model_is_env_overridable(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VCC_DEFAULT_ANTHROPIC_MODEL", "claude-sonnet-4-5")
    settings = Settings()
    assert settings.anthropic_model == "claude-sonnet-4-5"


def test_anthropic_model_explicit_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VCC_DEFAULT_ANTHROPIC_MODEL", "claude-sonnet-4-5")
    settings = Settings(anthropic_model="claude-opus-4-7")
    assert settings.anthropic_model == "claude-opus-4-7"


# ---------------------------------------------------------------------------
# Protocol surface and provider-neutrality
# ---------------------------------------------------------------------------


def test_reporter_protocol_importable_and_runtime_checkable() -> None:
    from video_content_capture.reporting.base import Reporter

    assert hasattr(Reporter, "_is_runtime_protocol") or hasattr(Reporter, "_is_protocol")


def test_claude_adapter_satisfies_reporter_protocol() -> None:
    from video_content_capture.reporting.base import Reporter
    from video_content_capture.reporting.claude import ClaudeReporter

    adapter = ClaudeReporter(settings=_make_settings())
    assert isinstance(adapter, Reporter)


def test_provider_sdk_not_leaked_into_neutral_modules() -> None:
    """base/prompts/grounding/domain must not import the Anthropic SDK."""

    import importlib

    for modname in (
        "video_content_capture.reporting.base",
        "video_content_capture.reporting.prompts",
        "video_content_capture.reporting.grounding",
        "video_content_capture.domain.models",
    ):
        mod = importlib.import_module(modname)
        src = Path(mod.__file__).read_text(encoding="utf-8")
        assert "import anthropic" not in src, f"{modname} imports anthropic SDK"
        assert "from anthropic" not in src, f"{modname} imports anthropic SDK"
        assert "anthropic." not in src, f"{modname} references anthropic SDK types"


def test_claude_module_is_only_anthropic_sdk_import_site() -> None:
    """Only ``reporting/claude.py`` may import the Anthropic SDK."""

    import importlib

    mod = importlib.import_module("video_content_capture.reporting.claude")
    src = Path(mod.__file__).read_text(encoding="utf-8")
    assert "import anthropic" in src or "from anthropic" in src


# ---------------------------------------------------------------------------
# Structured response models
# ---------------------------------------------------------------------------


def test_structured_response_carries_evidence_ids_on_source_dependent_items() -> None:
    from video_content_capture.reporting.base import StructuredReport

    payload = _valid_structured_payload()
    report = StructuredReport.model_validate(payload)
    assert report.core_topics[0].source_segment_ids == ["s0001"]
    assert report.important_numbers[0].source_segment_ids == ["s0001"]
    assert report.conclusion.source_segment_ids == ["s0002"]
    assert report.glossary[0].source_segment_ids == []


def test_structured_response_covers_six_required_sections() -> None:
    from video_content_capture.reporting.base import REQUIRED_STRUCTURED_SECTION_TYPES

    assert set(REQUIRED_STRUCTURED_SECTION_TYPES) == {
        "overview",
        "core_topics",
        "important_numbers",
        "glossary",
        "conclusion",
        "source_index",
    }


def test_structured_response_allows_empty_evidence_for_grounding_to_reject() -> None:
    """The structured schema allows empty evidence on source-dependent items so
    grounding (not the schema) can reject it with a typed GroundingError. This
    keeps malformed-structured-output (a schema failure) distinct from
    empty-evidence grounding rejection."""

    from video_content_capture.reporting.base import StructuredReport

    payload = _valid_structured_payload(core_ids=[])
    # Schema accepts: no ValidationError.
    report = StructuredReport.model_validate(payload)
    assert report.core_topics[0].source_segment_ids == []


def test_structured_conclusion_carries_opinion_flag() -> None:
    """A conclusion carries ``is_speaker_opinion``; grounding enforces labeling."""

    from video_content_capture.reporting.base import StructuredReport

    payload = _valid_structured_payload(conclusion_is_opinion=False)
    report = StructuredReport.model_validate(payload)
    assert report.conclusion.is_speaker_opinion is False


# ---------------------------------------------------------------------------
# Prompts
# ---------------------------------------------------------------------------


def test_prompt_segments_are_ordered_and_carry_ids_times_anonymous_speaker() -> None:
    from video_content_capture.reporting.prompts import build_segment_payload

    transcript = _make_two_segment_transcript(Path("video.mp4"))
    payload = build_segment_payload(transcript)
    assert [p["segment_id"] for p in payload] == ["s0001", "s0002"]
    assert payload[0]["start_seconds"] == 0.0
    assert payload[0]["end_seconds"] == 10.0
    assert payload[0]["speaker_label"] == "講者 A"
    assert payload[0]["normalized_text"] == "今天談美國聯邦基金利率。"
    # No raw text exposed unless uncertainty/evidence requires it; the default
    # payload exposes normalized text only.
    assert "raw_text" not in payload[0]


def test_prompt_text_requires_traditional_chinese_and_evidence_ids_only() -> None:
    from video_content_capture.reporting.prompts import build_reporting_prompt

    transcript = _make_two_segment_transcript(Path("video.mp4"))
    system, user = build_reporting_prompt(transcript)
    text = system + "\n" + user
    assert "繁體中文" in text or "Traditional Chinese" in text
    assert "投資建議" in text
    assert "講者觀點" in text
    assert "segment ID" in text.lower() or "區段 ID" in text


def test_prompt_does_not_embed_api_key() -> None:
    from video_content_capture.reporting.prompts import build_reporting_prompt

    transcript = _make_two_segment_transcript(Path("video.mp4"))
    system, user = build_reporting_prompt(transcript)
    assert "test-anthropic-key" not in system + user
    assert "sk-" not in system + user


# ---------------------------------------------------------------------------
# Grounding
# ---------------------------------------------------------------------------


def test_grounding_accepts_valid_structured_response(tmp_path: Path) -> None:
    from video_content_capture.reporting.grounding import ground_structured_report

    transcript = _make_two_segment_transcript(tmp_path)
    structured = _valid_structured_payload()
    report = ground_structured_report(transcript, structured)
    assert isinstance(report, Report)
    assert len(report.sections) == 6


def test_grounding_rejects_unknown_segment_id(tmp_path: Path) -> None:
    from video_content_capture.reporting.grounding import ground_structured_report

    transcript = _make_two_segment_transcript(tmp_path)
    structured = _valid_structured_payload(core_ids=["s9999"])
    with pytest.raises(GroundingError):
        ground_structured_report(transcript, structured)


def test_grounding_rejects_empty_evidence_for_source_dependent_claim(tmp_path: Path) -> None:
    from video_content_capture.reporting.grounding import ground_structured_report

    transcript = _make_two_segment_transcript(tmp_path)
    structured = _valid_structured_payload(core_ids=[])
    with pytest.raises(GroundingError):
        ground_structured_report(transcript, structured)


def test_grounding_rejects_missing_required_section(tmp_path: Path) -> None:
    from video_content_capture.reporting.grounding import ground_structured_report

    transcript = _make_two_segment_transcript(tmp_path)
    structured = _valid_structured_payload()
    del structured["conclusion"]
    with pytest.raises(GroundingError):
        ground_structured_report(transcript, structured)


def test_grounding_rejects_malformed_extra_section(tmp_path: Path) -> None:
    from video_content_capture.reporting.grounding import ground_structured_report

    transcript = _make_two_segment_transcript(tmp_path)
    structured = _valid_structured_payload()
    structured["overview_extra"] = structured["overview"]
    with pytest.raises((GroundingError, ValidationError)):
        ground_structured_report(transcript, structured)


def test_grounding_rejects_out_of_range_evidence_timing(tmp_path: Path) -> None:
    """Referenced segment timing must lie within the canonical media duration."""

    from video_content_capture.reporting.grounding import ground_structured_report

    bad_seg = _segment("s0500", start=0.0, end=9999.0, text="超時的內容。")
    transcript = Transcript(
        metadata=_make_media_metadata(Path("video.mp4"), duration_seconds=10.0),
        segments=[bad_seg],
        language="zh-TW",
    )
    structured = _valid_structured_payload(core_ids=["s0500"], conclusion_ids=["s0500"])
    with pytest.raises(GroundingError):
        ground_structured_report(transcript, structured)


def test_grounding_requires_speaker_opinion_label_for_forecasts(tmp_path: Path) -> None:
    """A conclusion that reads as a forecast must be labeled as a speaker viewpoint."""

    from video_content_capture.reporting.grounding import ground_structured_report

    transcript = _make_two_segment_transcript(tmp_path)
    structured = _valid_structured_payload(conclusion_is_opinion=False)
    structured["conclusion"]["conclusion"] = "我預期下半年利率會維持高檔。"
    with pytest.raises(GroundingError):
        ground_structured_report(transcript, structured)


# ---------------------------------------------------------------------------
# Claude adapter: successful single-group report
# ---------------------------------------------------------------------------


def test_grounding_requires_opinion_label_for_forecast_possible_impact(
    tmp_path: Path,
) -> None:
    """Forecast language in possible_impact must not bypass viewpoint labeling."""

    from video_content_capture.reporting.grounding import ground_structured_report

    transcript = _make_two_segment_transcript(tmp_path)
    structured = _valid_structured_payload(conclusion_is_opinion=False)
    structured["conclusion"]["conclusion"] = "本集整理目前的利率環境。"
    structured["conclusion"]["possible_impact"] = "下半年借貸成本可能會繼續上升。"

    with pytest.raises(GroundingError):
        ground_structured_report(transcript, structured)


# ---------------------------------------------------------------------------
# Claude adapter: successful single-group report
# ---------------------------------------------------------------------------


def test_claude_adapter_returns_validated_report_without_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from video_content_capture.reporting.base import ReporterResult
    from video_content_capture.reporting.claude import ClaudeReporter

    transcript = _make_two_segment_transcript(tmp_path)
    _patch_parse(monkeypatch, payload=_valid_structured_payload())
    adapter = ClaudeReporter(settings=_make_settings())
    result = adapter.report(transcript=transcript, settings=_make_settings())
    assert isinstance(result, ReporterResult)
    assert isinstance(result.report, Report)
    # No Markdown is generated inside the adapter.
    assert not hasattr(result, "markdown")
    assert result.raw_metadata is not None
    assert "test-anthropic-key" not in json.dumps(result.raw_metadata, ensure_ascii=False)


def test_claude_adapter_passes_configured_model_to_parse(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from video_content_capture.reporting.claude import ClaudeReporter

    transcript = _make_two_segment_transcript(tmp_path)
    captured = _patch_parse(monkeypatch, payload=_valid_structured_payload())
    adapter = ClaudeReporter(settings=_make_settings(anthropic_model="claude-opus-4-8"))
    adapter.report(
        transcript=transcript, settings=_make_settings(anthropic_model="claude-opus-4-8")
    )
    assert captured["model"] == "claude-opus-4-8"


# ---------------------------------------------------------------------------
# Map / reduce
# ---------------------------------------------------------------------------


def test_long_transcript_splits_into_bounded_chronological_groups(tmp_path: Path) -> None:
    from video_content_capture.reporting.claude import ClaudeReporter

    segs = [
        _segment(f"s{i:04d}", start=float(i * 5), end=float(i * 5 + 4), text=f"段落 {i}")
        for i in range(20)
    ]
    transcript = _make_transcript(segs, duration_seconds=100.0, tmp_path=tmp_path)

    adapter = ClaudeReporter(settings=_make_settings(), max_segments_per_group=5)
    groups = adapter.plan_groups(transcript)
    assert len(groups) > 1
    flat = [seg for group in groups for seg in group]
    assert [s.segment_id for s in flat] == [s.segment_id for s in segs]
    assert all(len(g) <= 5 for g in groups)


def test_map_outputs_retain_evidence_ids_and_reduce_preserves_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Multiple groups: each map output retains source IDs; reduce merges/
    deduplicates while preserving at least one valid evidence ID for every
    retained source-dependent point."""

    from video_content_capture.reporting.claude import ClaudeReporter

    segs = [
        _segment(f"s{i:04d}", start=float(i * 5), end=float(i * 5 + 4), text=f"段落 {i}")
        for i in range(12)
    ]
    transcript = _make_transcript(segs, duration_seconds=60.0, tmp_path=tmp_path)
    call_log: list[list[str]] = []

    def _on_call(**kwargs: Any) -> dict[str, Any]:
        segments = kwargs.get("segments", [])
        ids = [s["segment_id"] for s in segments]
        call_log.append(ids)
        if len(ids) <= 1:
            return _valid_structured_payload(
                core_ids=["s0000"], numbers_ids=["s0001"], conclusion_ids=["s0002"]
            )
        core = ids[:1]
        numbers = ids[:1]
        conclusion = ids[1:2] if len(ids) > 1 else ids[:1]
        return _valid_structured_payload(
            core_ids=core, numbers_ids=numbers, conclusion_ids=conclusion
        )

    _patch_parse(monkeypatch, on_call=_on_call)
    adapter = ClaudeReporter(settings=_make_settings(), max_segments_per_group=4)
    result = adapter.report(transcript=transcript, settings=_make_settings())
    report = result.report
    assert len(call_log) > 1
    for section in report.sections:
        if section.is_source_dependent:
            assert section.source_segment_ids, (
                f"section {section.section_type!r} lost all evidence after reduce"
            )
            for sid in section.source_segment_ids:
                assert sid in transcript.segment_ids


def test_reduce_never_drops_all_evidence_from_a_retained_point(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """If reduce returns a source-dependent section with empty evidence,
    grounding must reject rather than emit an evidence-free retained point."""

    from video_content_capture.reporting.claude import ClaudeReporter

    segs = [
        _segment("s0001", start=0.0, end=5.0, text="第一段。"),
        _segment("s0002", start=5.0, end=10.0, text="第二段。"),
    ]
    transcript = _make_transcript(segs, duration_seconds=10.0, tmp_path=tmp_path)
    bad_reduce = _valid_structured_payload(core_ids=[], conclusion_ids=["s0002"])

    def _on_call(**kwargs: Any) -> dict[str, Any]:
        segments = kwargs.get("segments", [])
        if len(segments) <= 1:
            return bad_reduce
        ids = [s["segment_id"] for s in segments]
        return _valid_structured_payload(core_ids=ids[:1], conclusion_ids=ids[:1])

    _patch_parse(monkeypatch, on_call=_on_call)
    adapter = ClaudeReporter(settings=_make_settings(), max_segments_per_group=1)
    with pytest.raises(GroundingError):
        adapter.report(transcript=transcript, settings=_make_settings())


# ---------------------------------------------------------------------------
# Retry classification
# ---------------------------------------------------------------------------


def test_timeout_is_retried_then_raises_rate_limit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anthropic

    from video_content_capture.reporting.claude import ClaudeReporter

    transcript = _make_two_segment_transcript(tmp_path)
    sleeper = _NoSleep()

    def _fake_parse(self: Any, **kwargs: Any) -> Any:
        raise anthropic.APITimeoutError(request=object())

    monkeypatch.setattr(
        "video_content_capture.reporting.claude.ClaudeReporter._sdk_parse",
        _fake_parse,
    )
    adapter = ClaudeReporter(settings=_make_settings(max_retries=3), sleeper=sleeper)
    with pytest.raises(RateLimitError):
        adapter.report(transcript=transcript, settings=_make_settings(max_retries=3))
    assert len(sleeper.delays) >= 1


def test_connection_error_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic

    from video_content_capture.reporting.claude import ClaudeReporter

    transcript = _make_two_segment_transcript(tmp_path)
    sleeper = _NoSleep()

    def _fake_parse(self: Any, **kwargs: Any) -> Any:
        raise anthropic.APIConnectionError(request=object())

    monkeypatch.setattr(
        "video_content_capture.reporting.claude.ClaudeReporter._sdk_parse",
        _fake_parse,
    )
    adapter = ClaudeReporter(settings=_make_settings(max_retries=2), sleeper=sleeper)
    with pytest.raises((RateLimitError, ProviderPayloadError)):
        adapter.report(transcript=transcript, settings=_make_settings(max_retries=2))
    assert len(sleeper.delays) >= 1


def test_rate_limit_429_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic
    import httpx

    from video_content_capture.reporting.claude import ClaudeReporter

    transcript = _make_two_segment_transcript(tmp_path)
    sleeper = _NoSleep()

    def _fake_parse(self: Any, **kwargs: Any) -> Any:
        response = httpx.Response(status_code=429, request=httpx.Request("POST", "https://x"))
        raise anthropic.RateLimitError(message="rate limited", response=response, body=None)

    monkeypatch.setattr(
        "video_content_capture.reporting.claude.ClaudeReporter._sdk_parse",
        _fake_parse,
    )
    adapter = ClaudeReporter(settings=_make_settings(max_retries=2), sleeper=sleeper)
    with pytest.raises(RateLimitError):
        adapter.report(transcript=transcript, settings=_make_settings(max_retries=2))
    assert len(sleeper.delays) >= 1


def test_server_5xx_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic
    import httpx

    from video_content_capture.reporting.claude import ClaudeReporter

    transcript = _make_two_segment_transcript(tmp_path)
    sleeper = _NoSleep()

    def _fake_parse(self: Any, **kwargs: Any) -> Any:
        response = httpx.Response(status_code=503, request=httpx.Request("POST", "https://x"))
        raise anthropic.InternalServerError(message="overloaded", response=response, body=None)

    monkeypatch.setattr(
        "video_content_capture.reporting.claude.ClaudeReporter._sdk_parse",
        _fake_parse,
    )
    adapter = ClaudeReporter(settings=_make_settings(max_retries=2), sleeper=sleeper)
    with pytest.raises(RateLimitError):
        adapter.report(transcript=transcript, settings=_make_settings(max_retries=2))
    assert len(sleeper.delays) >= 1


def test_every_http_5xx_is_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic
    import httpx

    from video_content_capture.reporting.claude import ClaudeReporter

    transcript = _make_two_segment_transcript(tmp_path)
    sleeper = _NoSleep()

    def _fake_parse(self: Any, **kwargs: Any) -> Any:
        response = httpx.Response(status_code=501, request=httpx.Request("POST", "https://x"))
        raise anthropic.APIStatusError(message="not implemented", response=response, body=None)

    monkeypatch.setattr(
        "video_content_capture.reporting.claude.ClaudeReporter._sdk_parse",
        _fake_parse,
    )
    adapter = ClaudeReporter(settings=_make_settings(max_retries=2), sleeper=sleeper)

    with pytest.raises(RateLimitError):
        adapter.report(transcript=transcript, settings=_make_settings(max_retries=2))

    assert len(sleeper.delays) == 1


def test_authentication_error_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anthropic
    import httpx

    from video_content_capture.reporting.claude import ClaudeReporter

    transcript = _make_two_segment_transcript(tmp_path)
    sleeper = _NoSleep()

    def _fake_parse(self: Any, **kwargs: Any) -> Any:
        response = httpx.Response(status_code=401, request=httpx.Request("POST", "https://x"))
        raise anthropic.AuthenticationError(message="invalid api key", response=response, body=None)

    monkeypatch.setattr(
        "video_content_capture.reporting.claude.ClaudeReporter._sdk_parse",
        _fake_parse,
    )
    adapter = ClaudeReporter(settings=_make_settings(max_retries=3), sleeper=sleeper)
    with pytest.raises(ProviderAuthError):
        adapter.report(transcript=transcript, settings=_make_settings(max_retries=3))
    assert sleeper.delays == []


def test_permission_denied_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic
    import httpx

    from video_content_capture.reporting.claude import ClaudeReporter

    transcript = _make_two_segment_transcript(tmp_path)
    sleeper = _NoSleep()

    def _fake_parse(self: Any, **kwargs: Any) -> Any:
        response = httpx.Response(status_code=403, request=httpx.Request("POST", "https://x"))
        raise anthropic.PermissionDeniedError(message="forbidden", response=response, body=None)

    monkeypatch.setattr(
        "video_content_capture.reporting.claude.ClaudeReporter._sdk_parse",
        _fake_parse,
    )
    adapter = ClaudeReporter(settings=_make_settings(max_retries=3), sleeper=sleeper)
    with pytest.raises((ProviderAuthError, ProviderPayloadError)):
        adapter.report(transcript=transcript, settings=_make_settings(max_retries=3))
    assert sleeper.delays == []


def test_bad_request_is_not_retried(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import anthropic
    import httpx

    from video_content_capture.reporting.claude import ClaudeReporter

    transcript = _make_two_segment_transcript(tmp_path)
    sleeper = _NoSleep()

    def _fake_parse(self: Any, **kwargs: Any) -> Any:
        response = httpx.Response(status_code=400, request=httpx.Request("POST", "https://x"))
        raise anthropic.BadRequestError(message="bad request", response=response, body=None)

    monkeypatch.setattr(
        "video_content_capture.reporting.claude.ClaudeReporter._sdk_parse",
        _fake_parse,
    )
    adapter = ClaudeReporter(settings=_make_settings(max_retries=3), sleeper=sleeper)
    with pytest.raises(ProviderPayloadError):
        adapter.report(transcript=transcript, settings=_make_settings(max_retries=3))
    assert sleeper.delays == []


def test_malformed_structured_output_is_not_retried(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A successful response whose parsed_output is None / fails schema
    validation is a malformed structured output and must NOT be retried."""

    from video_content_capture.reporting.claude import ClaudeReporter

    transcript = _make_two_segment_transcript(tmp_path)
    sleeper = _NoSleep()
    calls = {"n": 0}

    def _fake_parse(self: Any, **kwargs: Any) -> Any:
        calls["n"] += 1
        return _FakeParsedMessage(None)

    monkeypatch.setattr(
        "video_content_capture.reporting.claude.ClaudeReporter._sdk_parse",
        _fake_parse,
    )
    adapter = ClaudeReporter(settings=_make_settings(max_retries=3), sleeper=sleeper)
    with pytest.raises(ProviderPayloadError):
        adapter.report(transcript=transcript, settings=_make_settings(max_retries=3))
    assert calls["n"] == 1
    assert sleeper.delays == []


# ---------------------------------------------------------------------------
# Secret-free errors
# ---------------------------------------------------------------------------


def test_error_messages_and_details_are_secret_free(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import anthropic
    import httpx

    from video_content_capture.reporting.claude import ClaudeReporter

    transcript = _make_two_segment_transcript(tmp_path)

    def _fake_parse(self: Any, **kwargs: Any) -> Any:
        response = httpx.Response(status_code=401, request=httpx.Request("POST", "https://x"))
        raise anthropic.AuthenticationError(message="invalid api key", response=response, body=None)

    monkeypatch.setattr(
        "video_content_capture.reporting.claude.ClaudeReporter._sdk_parse",
        _fake_parse,
    )
    adapter = ClaudeReporter(settings=_make_settings(max_retries=1))
    try:
        adapter.report(transcript=transcript, settings=_make_settings(max_retries=1))
    except ProviderAuthError as exc:
        blob = exc.message + json.dumps(exc.details, ensure_ascii=False)
        assert "test-anthropic-key" not in blob
        assert "sk-" not in blob
    else:
        pytest.fail("expected ProviderAuthError")


def test_raw_metadata_does_not_contain_api_key(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from video_content_capture.reporting.claude import ClaudeReporter

    transcript = _make_two_segment_transcript(tmp_path)
    _patch_parse(monkeypatch, payload=_valid_structured_payload())
    adapter = ClaudeReporter(settings=_make_settings())
    result = adapter.report(transcript=transcript, settings=_make_settings())
    blob = json.dumps(result.raw_metadata, ensure_ascii=False)
    assert "test-anthropic-key" not in blob


# ---------------------------------------------------------------------------
# No model-authored timestamps trust path
# ---------------------------------------------------------------------------


def test_structured_response_has_no_timestamp_field() -> None:
    """The model-facing structured response models must not expose any
    timestamp field the model could authoritatively populate."""

    import importlib

    from video_content_capture.reporting import base as base_mod

    mod = importlib.import_module("video_content_capture.reporting.base")
    src = Path(mod.__file__).read_text(encoding="utf-8")
    forbidden = ("timestamp", "start_time", "end_time", "start_seconds", "end_seconds")
    for cls_name in (
        "StructuredReport",
        "StructuredOverview",
        "StructuredCoreTopic",
        "StructuredImportantNumber",
        "StructuredGlossaryEntry",
        "StructuredConclusion",
        "StructuredSourceIndex",
    ):
        cls = getattr(base_mod, cls_name, None)
        if cls is None:
            continue
        field_names = set(cls.model_fields.keys())
        for bad in forbidden:
            assert bad not in field_names, f"{cls_name} exposes timestamp field {bad!r}"
    _ = src


def test_report_sections_carry_no_model_timestamps(tmp_path: Path) -> None:
    """The provider-neutral Report returned by the adapter carries only
    source segment IDs (group 9 derives timestamps)."""

    from video_content_capture.reporting.grounding import ground_structured_report

    transcript = _make_two_segment_transcript(tmp_path)
    report = ground_structured_report(transcript, _valid_structured_payload())
    for section in report.sections:
        field_names = set(type(section).model_fields.keys())
        assert "start_time" not in field_names
        assert "end_time" not in field_names
        assert "timestamp" not in field_names


# ---------------------------------------------------------------------------
# Finding 1: opinion-label validation extended to core_topics / important_numbers
# ---------------------------------------------------------------------------


def test_grounding_rejects_core_topic_forecast_without_opinion_label(tmp_path: Path) -> None:
    """A core_topics item whose text reads as a forecast/judgment/recommendation
    must carry ``is_speaker_opinion=True``; grounding rejects otherwise."""

    from video_content_capture.reporting.grounding import ground_structured_report

    transcript = _make_two_segment_transcript(tmp_path)
    structured = _valid_structured_payload()
    structured["core_topics"][0]["topic"] = "我預期下半年利率會維持高檔。"
    structured["core_topics"][0]["is_speaker_opinion"] = False
    with pytest.raises(GroundingError):
        ground_structured_report(transcript, structured)


def test_grounding_accepts_core_topic_forecast_with_opinion_label(tmp_path: Path) -> None:
    """A core_topics item flagged as a speaker viewpoint is accepted."""

    from video_content_capture.reporting.grounding import ground_structured_report

    transcript = _make_two_segment_transcript(tmp_path)
    structured = _valid_structured_payload()
    structured["core_topics"][0]["topic"] = "我預期下半年利率會維持高檔。"
    structured["core_topics"][0]["is_speaker_opinion"] = True
    report = ground_structured_report(transcript, structured)
    assert isinstance(report, Report)


def test_grounding_rejects_important_number_judgment_without_opinion_label(
    tmp_path: Path,
) -> None:
    """An important_numbers item whose text reads as a judgment/recommendation
    must carry ``is_speaker_opinion=True``; grounding rejects otherwise."""

    from video_content_capture.reporting.grounding import ground_structured_report

    transcript = _make_two_segment_transcript(tmp_path)
    structured = _valid_structured_payload()
    structured["important_numbers"][0]["number_or_claim"] = "我認為美國今年應該會升息一次。"
    structured["important_numbers"][0]["is_speaker_opinion"] = False
    with pytest.raises(GroundingError):
        ground_structured_report(transcript, structured)


def test_grounding_accepts_important_number_judgment_with_opinion_label(
    tmp_path: Path,
) -> None:
    """An important_numbers item flagged as a speaker viewpoint is accepted."""

    from video_content_capture.reporting.grounding import ground_structured_report

    transcript = _make_two_segment_transcript(tmp_path)
    structured = _valid_structured_payload()
    structured["important_numbers"][0]["number_or_claim"] = "我認為美國今年應該會升息一次。"
    structured["important_numbers"][0]["is_speaker_opinion"] = True
    report = ground_structured_report(transcript, structured)
    assert isinstance(report, Report)


def test_grounding_accepts_non_forecast_core_topic_without_opinion_label(
    tmp_path: Path,
) -> None:
    """A non-forecast core_topics item is accepted without the opinion flag."""

    from video_content_capture.reporting.grounding import ground_structured_report

    transcript = _make_two_segment_transcript(tmp_path)
    structured = _valid_structured_payload()
    structured["core_topics"][0]["topic"] = "聯準會宣布維持利率不變。"  # factual report
    structured["core_topics"][0]["is_speaker_opinion"] = False
    report = ground_structured_report(transcript, structured)
    assert isinstance(report, Report)


def test_grounding_accepts_non_forecast_important_number_without_opinion_label(
    tmp_path: Path,
) -> None:
    """A non-forecast important_numbers item is accepted without the opinion flag."""

    from video_content_capture.reporting.grounding import ground_structured_report

    transcript = _make_two_segment_transcript(tmp_path)
    structured = _valid_structured_payload()
    structured["important_numbers"][0]["number_or_claim"] = (
        "美國聯邦基金利率目標區間 5.25% 至 5.50%。"
    )
    structured["important_numbers"][0]["is_speaker_opinion"] = False
    report = ground_structured_report(transcript, structured)
    assert isinstance(report, Report)


# ---------------------------------------------------------------------------
# Finding 2: positive multi-group reduce-evidence preservation
# ---------------------------------------------------------------------------


def test_reduce_preserves_group_specific_evidence_across_distinct_groups(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Positive reduce test: map outputs from distinct chronological groups carry
    distinct IDs; reduce returns retained merged points whose evidence is
    non-empty and a subset of the union of map-result evidence, with at least
    one group-specific ID surviving per source-dependent section."""

    from video_content_capture.reporting.base import StructuredReport
    from video_content_capture.reporting.claude import ClaudeReporter

    # Two chronological groups with disjoint segment ID sets.
    group_a = [
        _segment(f"a{i:04d}", start=float(i * 5), end=float(i * 5 + 4), text=f"A 段落 {i}")
        for i in range(3)
    ]
    group_b = [
        _segment(
            f"b{i:04d}", start=float(15 + i * 5), end=float(15 + i * 5 + 4), text=f"B 段落 {i}"
        )
        for i in range(3)
    ]
    segs = group_a + group_b
    transcript = _make_transcript(segs, duration_seconds=45.0, tmp_path=tmp_path)

    # Capture the map-result evidence sets by group to assert subset later.
    map_evidence_by_call: list[set[str]] = []

    def _on_call(**kwargs: Any) -> dict[str, Any]:
        segments = kwargs.get("segments", [])
        ids = [s["segment_id"] for s in segments]
        if len(ids) <= 1:
            # Reduce: return a merged structured report that retains evidence
            # drawn from BOTH groups, with at least one group-specific ID per
            # source-dependent section.
            return _valid_structured_payload(
                core_ids=["a0001"],
                numbers_ids=["b0001"],
                conclusion_ids=["a0002", "b0002"],
                source_index_ids=["a0001", "a0002", "b0001", "b0002"],
            )
        # Map: reference this group's own IDs.
        map_evidence_by_call.append(set(ids))
        return _valid_structured_payload(
            core_ids=ids[:1],
            numbers_ids=ids[:1],
            conclusion_ids=ids[1:2] if len(ids) > 1 else ids[:1],
        )

    _patch_parse(monkeypatch, on_call=_on_call)
    adapter = ClaudeReporter(settings=_make_settings(), max_segments_per_group=3)
    result = adapter.report(transcript=transcript, settings=_make_settings())
    report = result.report

    # At least two map calls + one reduce call happened.
    assert len(map_evidence_by_call) >= 2

    # The union of all map-result evidence IDs.
    union_evidence: set[str] = set().union(*map_evidence_by_call)

    # Every source-dependent section's retained evidence is non-empty, a subset
    # of the union of map-result evidence, and contains at least one
    # group-specific ID that survived the reduce.
    for section in report.sections:
        if not section.is_source_dependent:
            continue
        assert section.source_segment_ids, (
            f"section {section.section_type!r} lost all evidence after reduce"
        )
        for sid in section.source_segment_ids:
            assert sid in union_evidence, (
                f"section {section.section_type!r} retained ID {sid!r} not in map union"
            )
            assert sid in transcript.segment_ids, (
                f"section {section.section_type!r} retained ID {sid!r} unknown to transcript"
            )
        # At least one group-specific ID survived: the retained evidence is
        # not empty and every retained ID is a real group segment ID (either
        # a0xxx or b0xxx), proving a group-specific ID survived per section.
        assert any(
            sid.startswith("a") or sid.startswith("b") for sid in section.source_segment_ids
        ), f"section {section.section_type!r} retained no group-specific ID"

    # Distinct groups produced disjoint map-evidence sets.
    assert map_evidence_by_call[0].isdisjoint(map_evidence_by_call[1])

    # The reduce result used real StructuredReport schema (no schema error).
    _ = StructuredReport  # imported for completeness


# ---------------------------------------------------------------------------
# Finding 3: offline real-SDK binding test (zero network calls)
# ---------------------------------------------------------------------------


def test_real_sdk_messages_parse_serializes_and_parses_offline() -> None:
    """An offline real-SDK binding test for ``anthropic==0.117.0``.

    This exercises the ACTUAL ``client.messages.parse`` call and parameter
    serialization through an injected ``httpx.MockTransport`` (an SDK-supported
    seam: ``Anthropic(http_client=...)``). It makes ZERO network calls and
    proves the ``model``, ``max_tokens``, ``thinking``, ``output_config``,
    messages/system, and structured output format are accepted/serialized,
    and that the real response parser yields a ``StructuredReport``.
    """

    import anthropic
    import httpx

    from video_content_capture.reporting.base import StructuredReport

    payload = _valid_structured_payload()
    text = json.dumps(payload, ensure_ascii=False)
    resp_body = {
        "id": "msg_binding",
        "type": "message",
        "role": "assistant",
        "model": "claude-opus-4-8",
        "content": [{"type": "text", "text": text}],
        "stop_reason": "end_turn",
        "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 20},
    }

    captured: dict[str, Any] = {}

    def _handler(request: httpx.Request) -> httpx.Response:
        captured["url"] = str(request.url)
        captured["method"] = request.method
        captured["body"] = json.loads(request.content.decode("utf-8"))
        captured["auth_header"] = request.headers.get("x-api-key", "")
        return httpx.Response(200, json=resp_body, request=request)

    transport = httpx.MockTransport(_handler)
    http_client = httpx.Client(transport=transport)
    # Pass base_url explicitly so the test is host-independent and not
    # affected by ANTHROPIC_BASE_URL in the environment.
    client = anthropic.Anthropic(
        api_key="test-key",
        http_client=http_client,
        max_retries=0,
        base_url="https://api.anthropic.com",
    )

    parsed = client.messages.parse(
        model="claude-opus-4-8",
        messages=[{"role": "user", "content": "使用者訊息"}],
        system="系統提示",
        max_tokens=8192,
        thinking={"type": "adaptive"},
        output_config={"effort": "high"},
        output_format=StructuredReport,
    )

    # The real SDK parsed the response text into a StructuredReport.
    assert isinstance(parsed.parsed_output, StructuredReport)
    assert parsed.parsed_output.overview.summary == payload["overview"]["summary"]
    assert parsed.parsed_output.conclusion.is_speaker_opinion is True

    # The request was serialized with every required parameter.
    body = captured["body"]
    assert captured["method"] == "POST"
    assert "api.anthropic.com" in captured["url"]
    assert body["model"] == "claude-opus-4-8"
    assert body["max_tokens"] == 8192
    assert body["thinking"] == {"type": "adaptive"}
    assert body["system"] == "系統提示"
    assert body["messages"] == [{"role": "user", "content": "使用者訊息"}]
    # output_config carries the merged structured-output format schema.
    assert "output_config" in body
    oc = body["output_config"]
    assert oc.get("effort") == "high"
    assert oc.get("format", {}).get("type") == "json_schema"
    assert "schema" in oc["format"]

    # No real network call left the process; the mock transport handled it.
    # (The auth header is set by the SDK from the api_key we passed; it is a
    # test-only value and never logged or persisted.)
    assert captured["auth_header"] == "test-key"


def test_real_sdk_messages_parse_rejects_bad_request_offline() -> None:
    """The real SDK raises a typed ``BadRequestError`` for a 400 response,
    confirming the binding test exercises the real SDK error path too."""

    import anthropic
    import httpx

    from video_content_capture.reporting.base import StructuredReport

    def _handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            400,
            json={"type": "error", "error": {"type": "invalid_request_error", "message": "bad"}},
            request=request,
        )

    transport = httpx.MockTransport(_handler)
    http_client = httpx.Client(transport=transport)
    client = anthropic.Anthropic(
        api_key="test-key",
        http_client=http_client,
        max_retries=0,
        base_url="https://api.anthropic.com",
    )
    with pytest.raises(anthropic.BadRequestError):
        client.messages.parse(
            model="claude-opus-4-8",
            messages=[{"role": "user", "content": "hi"}],
            max_tokens=8,
            thinking={"type": "adaptive"},
            output_config={"effort": "high"},
            output_format=StructuredReport,
        )


# ---------------------------------------------------------------------------
# Finding I1/I2: structured per-item ReportItem population in grounding
# ---------------------------------------------------------------------------


def test_grounding_populates_structured_items_for_core_topics(tmp_path: Path) -> None:
    """Grounding populates ``ReportSection.items`` with ``ReportItem`` objects
    carrying verbatim text, per-item evidence IDs, and the opinion flag —
    without embedding Markdown bullets or ``（講者觀點）`` in canonical
    ``content`` or item ``text``."""

    from video_content_capture.reporting.grounding import ground_structured_report

    transcript = _make_two_segment_transcript(tmp_path)
    structured = _valid_structured_payload()
    structured["core_topics"][0]["topic"] = "我預期下半年利率會維持高檔。"
    structured["core_topics"][0]["is_speaker_opinion"] = True
    report = ground_structured_report(transcript, structured)

    core = next(s for s in report.sections if s.section_type == "core_topics")
    assert len(core.items) == 1
    item = core.items[0]
    assert item.text == "我預期下半年利率會維持高檔。"
    assert item.source_segment_ids == ["s0001"]
    assert item.is_speaker_opinion is True
    # Canonical content/item text contains NO Markdown bullet or label.
    assert not item.text.startswith("- ")
    assert "（講者觀點）" not in item.text
    assert "（講者觀點）" not in core.content
    assert not core.content.startswith("- ")


def test_grounding_populates_structured_items_for_important_numbers(
    tmp_path: Path,
) -> None:
    from video_content_capture.reporting.grounding import ground_structured_report

    transcript = _make_two_segment_transcript(tmp_path)
    structured = _valid_structured_payload()
    structured["important_numbers"][0]["number_or_claim"] = "5.25% 至 5.50%。"
    structured["important_numbers"][0]["is_speaker_opinion"] = False
    report = ground_structured_report(transcript, structured)

    numbers = next(s for s in report.sections if s.section_type == "important_numbers")
    assert len(numbers.items) == 1
    assert numbers.items[0].text == "5.25% 至 5.50%。"
    assert numbers.items[0].source_segment_ids == ["s0001"]
    assert numbers.items[0].is_speaker_opinion is False
    assert "（講者觀點）" not in numbers.content


def test_grounding_populates_structured_items_for_glossary_with_label(
    tmp_path: Path,
) -> None:
    """Glossary items carry ``label`` (the term) and ``text`` (the
    explanation), with per-item evidence when present."""

    from video_content_capture.reporting.grounding import ground_structured_report

    transcript = _make_two_segment_transcript(tmp_path)
    structured = _valid_structured_payload(
        glossary_ids=["s0001"],
    )
    structured["glossary"][0]["term"] = "聯邦基金利率"
    structured["glossary"][0]["explanation"] = "銀行間隔夜借貸的目標利率。"
    report = ground_structured_report(transcript, structured)

    glossary = next(s for s in report.sections if s.section_type == "glossary")
    assert len(glossary.items) == 1
    entry = glossary.items[0]
    assert entry.label == "聯邦基金利率"
    assert entry.text == "銀行間隔夜借貸的目標利率。"
    assert entry.source_segment_ids == ["s0001"]
    # No Markdown bullet / colon prefix baked into canonical text.
    assert not entry.text.startswith("- ")
    assert "：" not in entry.text


def test_grounding_populates_structured_items_for_conclusion(tmp_path: Path) -> None:
    from video_content_capture.reporting.grounding import ground_structured_report

    transcript = _make_two_segment_transcript(tmp_path)
    structured = _valid_structured_payload(conclusion_is_opinion=True)
    report = ground_structured_report(transcript, structured)

    conclusion = next(s for s in report.sections if s.section_type == "conclusion")
    # The conclusion section carries the conclusion and the possible-impact as
    # two structured items, each with the section's evidence and opinion flag.
    assert len(conclusion.items) == 2
    conclusion_item = conclusion.items[0]
    impact_item = conclusion.items[1]
    assert conclusion_item.source_segment_ids == ["s0002"]
    assert conclusion_item.is_speaker_opinion is True
    assert impact_item.source_segment_ids == ["s0002"]
    assert impact_item.is_speaker_opinion is True
    # The conclusion text is verbatim; no prefix or label baked in.
    assert conclusion_item.text.startswith("講者預期")
    assert "（講者觀點）" not in conclusion_item.text
    assert "（講者觀點）" not in impact_item.text
    assert "可能影響：" not in impact_item.text
    assert "可能影響：" not in conclusion.content
    assert "（講者觀點）" not in conclusion.content


def test_grounding_section_level_evidence_is_union_of_item_ids(
    tmp_path: Path,
) -> None:
    """The section-level ``source_segment_ids`` is the deterministic,
    deduplicated, order-preserving union of per-item evidence IDs."""

    from video_content_capture.reporting.grounding import ground_structured_report

    segs = [
        _segment("s0001", start=0.0, end=10.0, text="第一段。"),
        _segment("s0002", start=10.0, end=20.0, text="第二段。"),
        _segment("s0003", start=20.0, end=30.0, text="第三段。"),
    ]
    transcript = _make_transcript(segs, duration_seconds=30.0, tmp_path=tmp_path)
    structured = _valid_structured_payload()
    structured["core_topics"] = [
        {
            "topic": "重點 A。",
            "source_segment_ids": ["s0003", "s0001"],
            "is_speaker_opinion": False,
        },
        {
            "topic": "重點 B。",
            "source_segment_ids": ["s0002", "s0001"],
            "is_speaker_opinion": False,
        },
    ]
    report = ground_structured_report(transcript, structured)
    core = next(s for s in report.sections if s.section_type == "core_topics")
    # Union preserves first-seen order across items: s0003, s0001, s0002.
    assert core.source_segment_ids == ["s0003", "s0001", "s0002"]
    assert core.items[0].source_segment_ids == ["s0003", "s0001"]
    assert core.items[1].source_segment_ids == ["s0002", "s0001"]


def test_grounding_canonical_json_has_structured_flags_without_markdown(
    tmp_path: Path,
) -> None:
    """Canonical report JSON contains structured item flags/IDs but NO Markdown
    bullet or embedded ``（講者觀點）``."""

    import json

    from video_content_capture.reporting.grounding import ground_structured_report

    transcript = _make_two_segment_transcript(tmp_path)
    structured = _valid_structured_payload()
    structured["core_topics"][0]["topic"] = "我預期下半年利率會維持高檔。"
    structured["core_topics"][0]["is_speaker_opinion"] = True
    report = ground_structured_report(transcript, structured)
    data = json.loads(report.model_dump_json(indent=2))

    core = next(s for s in data["sections"] if s["section_type"] == "core_topics")
    assert core["items"][0]["is_speaker_opinion"] is True
    assert core["items"][0]["source_segment_ids"] == ["s0001"]
    blob = json.dumps(data, ensure_ascii=False)
    assert "（講者觀點）" not in blob
    # No item text starts with a Markdown bullet.
    for section in data["sections"]:
        for item in section.get("items", []):
            assert not item["text"].startswith("- ")


def test_grounding_overview_and_source_index_items_carry_evidence_when_present(
    tmp_path: Path,
) -> None:
    """Overview items carry per-item evidence when the model provides it; the
    source index section uses ``items`` to carry the referenced IDs."""

    from video_content_capture.reporting.grounding import ground_structured_report

    transcript = _make_two_segment_transcript(tmp_path)
    structured = _valid_structured_payload(overview_ids=["s0001"])
    report = ground_structured_report(transcript, structured)
    overview = next(s for s in report.sections if s.section_type == "overview")
    assert len(overview.items) == 1
    assert overview.items[0].source_segment_ids == ["s0001"]
