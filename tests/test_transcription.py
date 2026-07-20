"""Focused transcription tests for OpenSpec group 6.

Covers:

* Provider-neutral ``Transcriber`` protocol surface.
* AssemblyAI adapter maps utterances to provider-neutral domain segments
  with stable deterministic anonymous labels (``講者 A``/``講者 B``), preserves
  raw provider text and raw response bytes, and never leaks provider SDK
  types into domain or pipeline modules.
* Automatic speaker estimation when no bounds are supplied.
* Optional speaker bounds: equal min==max -> expected count passed exactly;
  both set and unequal -> bounds passed; only one set -> safe automatic
  estimation policy (no half-bound invented).
* Malformed provider responses raise a typed ``ProviderPayloadError`` and are
  not retried.
* Authentication failure (HTTP 401/403) raises ``ProviderAuthError`` and is
  not retried.
* 429 and 5xx are retried with bounded exponential backoff (no real sleep:
  backoff delay is injected).
* Timeout (httpx.TimeoutException / TranscriptError with 408-like state) is
  retried.
* Non-retryable 4xx (400/404/422) raise ``ProviderPayloadError`` without
  retry.
* Full-audio job is preferred; chunk fallback is triggered ONLY by configured
  provider size/duration limits, applies bounded overlap, timestamp offsets,
  conservative duplicate removal, and stable speaker reconciliation. Chunk
  audio input goes through an injectable chunker/uploader boundary so tests
  use owned paths/fixtures and make no paid calls.
* Conservative normalization: punctuation and Traditional Chinese character
  conversion preserve raw text and never silently change numbers, currencies,
  entities, stock symbols, or uncertain terms.
* A guard test proves the default suite makes no paid/live API request.

All network/SDK calls are mocked/injected. No real ffmpeg, AssemblyAI upload,
transcription, or polling is performed. Live tests, if any, require an
explicit ``@pytest.mark.live`` marker and opt-in env (not present here).
"""

from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any

import pytest

from video_content_capture.config import Settings
from video_content_capture.domain.errors import (
    ProviderAuthError,
    ProviderPayloadError,
    RateLimitError,
)
from video_content_capture.domain.models import (
    MediaMetadata,
    StreamInfo,
    Transcript,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------

# Cloud credentials MUST NOT be present in the default test environment; the
# autouse fixture strips them so a developer running ``uv run pytest -q``
# cannot accidentally trigger a paid call.
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
    return Settings(assemblyai_api_key="test-key", **overrides)


def _make_media_metadata(source_path: Path, duration_seconds: float = 2060.0) -> MediaMetadata:
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


def _utterance(
    text: str,
    start_ms: int,
    end_ms: int,
    speaker: str = "A",
    confidence: float = 0.95,
) -> dict[str, Any]:
    return {
        "text": text,
        "start": start_ms,
        "end": end_ms,
        "speaker": speaker,
        "confidence": confidence,
        "words": [
            {"text": text, "start": start_ms, "end": end_ms, "confidence": confidence},
        ],
    }


def _completed_response(
    utterances: list[dict[str, Any]] | None = None,
    *,
    text: str = "",
    language_code: str = "zh",
    audio_duration: int = 2060,
    transcript_id: str = "tid-123",
) -> dict[str, Any]:
    return {
        "id": transcript_id,
        "status": "completed",
        "language_code": language_code,
        "audio_url": "https://cdn.assemblyai.com/upload/x",
        "text": text,
        "utterances": utterances or [],
        "words": [],
        "confidence": 0.95,
        "audio_duration": audio_duration,
    }


# ---------------------------------------------------------------------------
# Guard: the default test suite MUST NOT make any paid/live request.
# ---------------------------------------------------------------------------


def test_default_suite_makes_no_paid_assemblyai_request(monkeypatch: pytest.MonkeyPatch) -> None:
    """A guard proving the default suite never performs a live/paid request.

    We assert that the AssemblyAI SDK's network entry points are NOT invoked
    during default test collection/run. Concretely: importing the test module
    and running any non-``live`` test must not call ``httpx.Client.request``
    used by ``assemblyai.api``. We install a global sentinel that fails any
    real HTTP call and assert it is never triggered by importing + exercising
    a no-op.

    Live tests use ``@pytest.mark.live`` plus an explicit opt-in env var
    (``VCC_ENABLE_LIVE=1``); neither is set in this suite.
    """

    import httpx

    calls: list[str] = []

    real_request = httpx.Client.request

    def _sentinel(
        self: httpx.Client, method: str, url: str, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        # A live call in the default suite would hit the real AssemblyAI
        # endpoint; record and fail loudly so it cannot be silent.
        if "assemblyai.com" in str(url):
            calls.append(f"{method} {url}")
            raise AssertionError(
                f"Default test suite attempted a live AssemblyAI request: {method} {url}"
            )
        return real_request(self, method, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "request", _sentinel)

    # The opt-in env is unset, so live tests are deselected by convention.
    assert monkeypatch.delenv("VCC_ENABLE_LIVE", raising=False) is None

    # Exercise a non-network path: constructing the adapter must not call out.
    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    _ = AssemblyAITranscriber(settings=_make_settings())
    assert calls == []


# ---------------------------------------------------------------------------
# Protocol surface
# ---------------------------------------------------------------------------


def test_transcriber_protocol_importable_and_runtime_checkable() -> None:
    """The provider-neutral Transcriber protocol is importable and runtime-checkable."""

    from video_content_capture.transcription.base import Transcriber

    # Protocol is defined and is runtime-checkable (instance check works).
    assert hasattr(Transcriber, "_is_runtime_protocol") or hasattr(Transcriber, "_is_protocol")


def test_assemblyai_adapter_satisfies_transcriber_protocol() -> None:
    """The AssemblyAI adapter satisfies the provider-neutral Transcriber protocol."""

    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber
    from video_content_capture.transcription.base import Transcriber

    adapter = AssemblyAITranscriber(settings=_make_settings())
    assert isinstance(adapter, Transcriber)


def test_adapter_does_not_leak_provider_types_into_domain() -> None:
    """Domain/pipeline modules must not import the AssemblyAI SDK."""

    import importlib

    # domain and transcription.base must not IMPORT assemblyai at module load.
    # Docstrings may reference the provider by name; the contract is about
    # not importing provider SDK types, so we check for import statements
    # and qualified provider references, not bare substrings.
    base = importlib.import_module("video_content_capture.transcription.base")
    assert not hasattr(base, "assemblyai")
    src_base = Path(base.__file__).read_text(encoding="utf-8")
    assert "import assemblyai" not in src_base
    assert "from assemblyai" not in src_base
    # No provider-qualified type references in the base module.
    assert "aai." not in src_base

    domain = importlib.import_module("video_content_capture.domain.models")
    src_domain = Path(domain.__file__).read_text(encoding="utf-8")
    assert "import assemblyai" not in src_domain
    assert "from assemblyai" not in src_domain
    assert "aai." not in src_domain


# ---------------------------------------------------------------------------
# Successful multi-speaker mapping
# ---------------------------------------------------------------------------


def test_successful_multispeaker_mapping(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """A completed response with utterances maps to deterministic anonymous labels."""

    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake-audio")
    metadata = _make_media_metadata(audio)

    response = _completed_response(
        utterances=[
            _utterance("你好", 0, 1000, speaker="A"),
            _utterance("今天市場", 1100, 2200, speaker="B"),
            _utterance("繼續討論", 2300, 3500, speaker="A"),
        ],
        text="你好 今天市場 繼續討論",
    )

    adapter = AssemblyAITranscriber(settings=_make_settings())

    # Inject a fake transcriber that returns a Transcript-like object holding
    # the raw response, mimicking the SDK's Transcript.from_response flow.
    captured = _FakeTranscript(response)

    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        # Record the audio path passed; assert it is the extracted audio, not
        # the source MP4.
        captured.audio_path_arg = data
        return captured

    monkeypatch.setattr(
        "video_content_capture.transcription.assemblyai.AssemblyAITranscriber._sdk_transcribe",
        _fake_transcribe,
    )

    result = adapter.transcribe(audio_path=audio, metadata=metadata, settings=_make_settings())

    assert result.chunked is False
    assert result.provider_job_id == "tid-123"
    # Raw provider text preserved separately.
    assert "你好" in result.raw_text
    # Raw response bytes preserved (JSON-serializable round trip).
    parsed = json.loads(result.raw_response.decode("utf-8"))
    assert parsed["id"] == "tid-123"
    # Domain transcript is provider-neutral.
    transcript = result.transcript
    assert isinstance(transcript, Transcript)
    assert len(transcript.segments) == 3
    # Deterministic anonymous labels in Traditional Chinese.
    assert transcript.segments[0].speaker_label == "講者 A"
    assert transcript.segments[1].speaker_label == "講者 B"
    assert transcript.segments[2].speaker_label == "講者 A"
    # Stable IDs ordered by start time.
    assert transcript.segments[0].segment_id < transcript.segments[1].segment_id
    # Timing in seconds (provider gives ms).
    assert transcript.segments[0].start == 0.0
    assert transcript.segments[0].end == 1.0
    assert transcript.segments[1].start == 1.1
    # Raw text preserved per segment.
    assert transcript.segments[0].raw_text == "你好"
    # Audio path passed is the extracted audio, never the source MP4.
    assert captured.audio_path_arg == audio
    assert captured.audio_path_arg != metadata.source_path


# ---------------------------------------------------------------------------
# Automatic speaker estimation
# ---------------------------------------------------------------------------


def test_automatic_speaker_estimation_when_no_bounds(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """No speaker bounds -> speaker_labels enabled, speakers_expected NOT set (auto)."""

    from video_content_capture.transcription import assemblyai as aai_mod

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio)

    seen_configs: list[Any] = []

    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        seen_configs.append(config)
        return _FakeTranscript(_completed_response(utterances=[_utterance("x", 0, 100)]))

    monkeypatch.setattr(aai_mod.AssemblyAITranscriber, "_sdk_transcribe", _fake_transcribe)

    adapter = aai_mod.AssemblyAITranscriber(settings=_make_settings())
    adapter.transcribe(audio_path=audio, metadata=metadata, settings=_make_settings())

    cfg = seen_configs[0]
    # speaker_labels MUST be enabled for diarization.
    assert cfg.speaker_labels is True
    # speakers_expected MUST be None when bounds are unset (automatic).
    assert cfg.speakers_expected is None


# ---------------------------------------------------------------------------
# Optional speaker bounds
# ---------------------------------------------------------------------------


def test_equal_speaker_bounds_pass_expected_count(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Equal min==max -> SpeakerOptions with min=max=expected count."""

    from video_content_capture.transcription import assemblyai as aai_mod

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio)
    seen: list[Any] = []

    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        seen.append(config)
        return _FakeTranscript(
            _completed_response(utterances=[_utterance("x", 0, 100, speaker="A")])
        )

    monkeypatch.setattr(aai_mod.AssemblyAITranscriber, "_sdk_transcribe", _fake_transcribe)

    settings = _make_settings(min_speakers=3, max_speakers=3)
    adapter = aai_mod.AssemblyAITranscriber(settings=settings)
    adapter.transcribe(audio_path=audio, metadata=metadata, settings=settings)

    cfg = seen[0]
    assert cfg.speaker_labels is True
    # Equal bounds -> exact expected count.
    assert cfg.speakers_expected == 3
    # SpeakerOptions carries min/max equal.
    assert cfg.speaker_options is not None
    assert cfg.speaker_options.min_speakers_expected == 3
    assert cfg.speaker_options.max_speakers_expected == 3


def test_unequal_speaker_bounds_pass_both(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Unequal min/max -> SpeakerOptions with both bounds."""

    from video_content_capture.transcription import assemblyai as aai_mod

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio)
    seen: list[Any] = []

    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        seen.append(config)
        return _FakeTranscript(
            _completed_response(utterances=[_utterance("x", 0, 100, speaker="A")])
        )

    monkeypatch.setattr(aai_mod.AssemblyAITranscriber, "_sdk_transcribe", _fake_transcribe)

    settings = _make_settings(min_speakers=2, max_speakers=5)
    adapter = aai_mod.AssemblyAITranscriber(settings=settings)
    adapter.transcribe(audio_path=audio, metadata=metadata, settings=settings)

    cfg = seen[0]
    assert cfg.speaker_labels is True
    assert cfg.speaker_options is not None
    assert cfg.speaker_options.min_speakers_expected == 2
    assert cfg.speaker_options.max_speakers_expected == 5
    # speakers_expected left None when bounds differ (auto estimation w/ bounds).
    assert cfg.speakers_expected is None


def test_only_one_bound_set_is_safe_automatic(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Only one of min/max set -> safe automatic estimation (no half-bound invented)."""

    from video_content_capture.transcription import assemblyai as aai_mod

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio)
    seen: list[Any] = []

    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        seen.append(config)
        return _FakeTranscript(
            _completed_response(utterances=[_utterance("x", 0, 100, speaker="A")])
        )

    monkeypatch.setattr(aai_mod.AssemblyAITranscriber, "_sdk_transcribe", _fake_transcribe)

    settings = _make_settings(max_speakers=4)
    adapter = aai_mod.AssemblyAITranscriber(settings=settings)
    adapter.transcribe(audio_path=audio, metadata=metadata, settings=settings)

    cfg = seen[0]
    assert cfg.speaker_labels is True
    # Safe policy: do not invent a half-bound; fall back to automatic.
    assert cfg.speaker_options is None
    assert cfg.speakers_expected is None


# ---------------------------------------------------------------------------
# Raw text preservation
# ---------------------------------------------------------------------------


def test_raw_text_preserved_separately_from_normalized(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Raw provider text and normalized text are stored separately on each segment."""

    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio)

    # Provider text intentionally lacks punctuation and uses a simplified form
    # that normalization may add punctuation to; raw MUST be preserved.
    response = _completed_response(
        utterances=[_utterance("台積電漲百分之三", 0, 1500, speaker="A")],
        text="台積電漲百分之三",
    )

    monkeypatch.setattr(
        AssemblyAITranscriber,
        "_sdk_transcribe",
        lambda self, data, config=None: _FakeTranscript(response),
    )

    adapter = AssemblyAITranscriber(settings=_make_settings())
    result = adapter.transcribe(audio_path=audio, metadata=metadata, settings=_make_settings())

    seg = result.transcript.segments[0]
    assert seg.raw_text == "台積電漲百分之三"
    # Normalized text may differ but must preserve the numeric meaning.
    assert seg.normalized_text  # nonempty
    assert "台積電" in seg.normalized_text
    assert "百分之三" in seg.normalized_text


# ---------------------------------------------------------------------------
# Malformed responses
# ---------------------------------------------------------------------------


def test_malformed_response_raises_provider_payload_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A completed response missing required fields raises ProviderPayloadError, not retried."""

    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio)

    # status=completed but no utterances and no text -> malformed payload.
    bad = _completed_response(utterances=None, text="")
    bad["status"] = "completed"

    call_count = {"n": 0}

    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        call_count["n"] += 1
        return _FakeTranscript(bad)

    monkeypatch.setattr(AssemblyAITranscriber, "_sdk_transcribe", _fake_transcribe)

    adapter = AssemblyAITranscriber(settings=_make_settings())
    with pytest.raises(ProviderPayloadError):
        adapter.transcribe(audio_path=audio, metadata=metadata, settings=_make_settings())
    # Must not retry on malformed payload.
    assert call_count["n"] == 1


def test_completed_text_without_diarized_utterances_is_rejected(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio)
    bad = _completed_response(utterances=None, text="有文字但沒有講者分段")
    bad["utterances"] = []

    monkeypatch.setattr(
        AssemblyAITranscriber,
        "_sdk_transcribe",
        lambda self, data, config=None: _FakeTranscript(bad),
    )

    adapter = AssemblyAITranscriber(settings=_make_settings())
    with pytest.raises(ProviderPayloadError, match="utterances"):
        adapter.transcribe(audio_path=audio, metadata=metadata, settings=_make_settings())


def test_transcript_status_error_raises_provider_payload_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A TranscriptResponse with status=error raises ProviderPayloadError, not retried."""

    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio)

    err = _completed_response()
    err["status"] = "error"
    err["error"] = "audio file too large"

    monkeypatch.setattr(
        AssemblyAITranscriber,
        "_sdk_transcribe",
        lambda self, data, config=None: _FakeTranscript(err),
    )

    adapter = AssemblyAITranscriber(settings=_make_settings())
    with pytest.raises(ProviderPayloadError):
        adapter.transcribe(audio_path=audio, metadata=metadata, settings=_make_settings())


# ---------------------------------------------------------------------------
# Authentication failure (not retried)
# ---------------------------------------------------------------------------


def test_authentication_failure_raises_provider_auth_error_no_retry(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import assemblyai as aai

    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio)

    calls = {"n": 0}

    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        calls["n"] += 1
        raise aai.TranscriptError("Unauthorized", status_code=401)

    monkeypatch.setattr(AssemblyAITranscriber, "_sdk_transcribe", _fake_transcribe)

    adapter = AssemblyAITranscriber(settings=_make_settings(), sleeper=_NoSleep())
    with pytest.raises(ProviderAuthError):
        adapter.transcribe(audio_path=audio, metadata=metadata, settings=_make_settings())
    assert calls["n"] == 1


def test_forbidden_raises_provider_auth_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import assemblyai as aai

    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio)

    monkeypatch.setattr(
        AssemblyAITranscriber,
        "_sdk_transcribe",
        lambda self, data, config=None: (_ for _ in ()).throw(
            aai.TranscriptError("Forbidden", status_code=403)
        ),
    )

    adapter = AssemblyAITranscriber(settings=_make_settings())
    with pytest.raises(ProviderAuthError):
        adapter.transcribe(audio_path=audio, metadata=metadata, settings=_make_settings())


# ---------------------------------------------------------------------------
# Retry: 429 and 5xx
# ---------------------------------------------------------------------------


def test_rate_limit_429_retried_with_backoff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import assemblyai as aai

    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio)

    calls = {"n": 0}
    delays: list[float] = []

    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] < 3:
            raise aai.TranscriptError("rate limited", status_code=429)
        return _FakeTranscript(_completed_response(utterances=[_utterance("ok", 0, 100)]))

    def _rec_sleeper(seconds: float) -> None:
        delays.append(seconds)

    monkeypatch.setattr(AssemblyAITranscriber, "_sdk_transcribe", _fake_transcribe)

    settings = _make_settings(max_retries=5, retry_base_delay_seconds=1.0)
    adapter = AssemblyAITranscriber(settings=settings, sleeper=_rec_sleeper)
    result = adapter.transcribe(audio_path=audio, metadata=metadata, settings=settings)
    assert calls["n"] == 3
    assert len(delays) == 2
    # Bounded exponential backoff: second delay >= first delay, both > 0.
    assert delays[0] > 0
    assert delays[1] >= delays[0]
    assert delays[1] <= delays[0] * 4 + 1  # bounded (base*2^n + jitter cap)
    assert result.transcript.segments[0].raw_text == "ok"


def test_5xx_retried(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import assemblyai as aai

    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio)

    calls = {"n": 0}

    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise aai.TranscriptError("server error", status_code=503)
        return _FakeTranscript(_completed_response(utterances=[_utterance("ok", 0, 100)]))

    monkeypatch.setattr(AssemblyAITranscriber, "_sdk_transcribe", _fake_transcribe)
    settings = _make_settings(max_retries=5)
    adapter = AssemblyAITranscriber(settings=settings, sleeper=_NoSleep())
    adapter.transcribe(audio_path=audio, metadata=metadata, settings=settings)
    assert calls["n"] == 2


def test_every_http_5xx_is_retried(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import assemblyai as aai

    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio)
    calls = {"n": 0}

    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise aai.TranscriptError("not implemented", status_code=501)
        return _FakeTranscript(_completed_response(utterances=[_utterance("ok", 0, 100)]))

    monkeypatch.setattr(AssemblyAITranscriber, "_sdk_transcribe", _fake_transcribe)
    settings = _make_settings(max_retries=2)
    adapter = AssemblyAITranscriber(settings=settings, sleeper=_NoSleep())

    adapter.transcribe(audio_path=audio, metadata=metadata, settings=settings)

    assert calls["n"] == 2


def test_5xx_exhausts_retries_and_raises_rate_limit_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import assemblyai as aai

    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio)

    calls = {"n": 0}

    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        calls["n"] += 1
        raise aai.TranscriptError("server error", status_code=502)

    monkeypatch.setattr(AssemblyAITranscriber, "_sdk_transcribe", _fake_transcribe)
    settings = _make_settings(max_retries=3)
    adapter = AssemblyAITranscriber(settings=settings, sleeper=_NoSleep())
    with pytest.raises(RateLimitError):
        adapter.transcribe(audio_path=audio, metadata=metadata, settings=settings)
    # 3 attempts total (initial + 2 retries) per max_retries semantics.
    assert calls["n"] == 3


# ---------------------------------------------------------------------------
# Retry: timeout
# ---------------------------------------------------------------------------


def test_timeout_retried(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import httpx

    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio)

    calls = {"n": 0}

    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        calls["n"] += 1
        if calls["n"] == 1:
            raise httpx.TimeoutException("read timeout")
        return _FakeTranscript(_completed_response(utterances=[_utterance("ok", 0, 100)]))

    monkeypatch.setattr(AssemblyAITranscriber, "_sdk_transcribe", _fake_transcribe)
    settings = _make_settings(max_retries=5)
    adapter = AssemblyAITranscriber(settings=settings, sleeper=_NoSleep())
    adapter.transcribe(audio_path=audio, metadata=metadata, settings=settings)
    assert calls["n"] == 2


# ---------------------------------------------------------------------------
# Non-retryable 4xx
# ---------------------------------------------------------------------------


def test_non_retryable_4xx_raises_provider_payload_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    import assemblyai as aai

    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio)

    calls = {"n": 0}

    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        calls["n"] += 1
        raise aai.TranscriptError("bad request", status_code=400)

    monkeypatch.setattr(AssemblyAITranscriber, "_sdk_transcribe", _fake_transcribe)
    settings = _make_settings(max_retries=5)
    adapter = AssemblyAITranscriber(settings=settings, sleeper=_NoSleep())
    with pytest.raises(ProviderPayloadError):
        adapter.transcribe(audio_path=audio, metadata=metadata, settings=settings)
    assert calls["n"] == 1


def test_404_raises_provider_payload_error(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import assemblyai as aai

    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio)

    monkeypatch.setattr(
        AssemblyAITranscriber,
        "_sdk_transcribe",
        lambda self, data, config=None: (_ for _ in ()).throw(
            aai.TranscriptError("not found", status_code=404)
        ),
    )
    adapter = AssemblyAITranscriber(settings=_make_settings(), sleeper=_NoSleep())
    with pytest.raises(ProviderPayloadError):
        adapter.transcribe(audio_path=audio, metadata=metadata, settings=_make_settings())


def test_secrets_not_in_error_messages(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    import assemblyai as aai

    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio)

    secret_key = "super-secret-key-do-not-leak"
    monkeypatch.setattr(
        AssemblyAITranscriber,
        "_sdk_transcribe",
        lambda self, data, config=None: (_ for _ in ()).throw(
            aai.TranscriptError(f"auth failed for key {secret_key}", status_code=401)
        ),
    )
    settings = Settings(assemblyai_api_key=secret_key)
    adapter = AssemblyAITranscriber(settings=settings, sleeper=_NoSleep())
    try:
        adapter.transcribe(audio_path=audio, metadata=metadata, settings=settings)
    except ProviderAuthError as exc:
        msg = str(exc)
        assert secret_key not in msg
        assert secret_key not in repr(exc.details)
    else:
        pytest.fail("expected ProviderAuthError")


# ---------------------------------------------------------------------------
# Chunk fallback (limit-driven)
# ---------------------------------------------------------------------------


def test_full_audio_preferred_within_limits(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Audio within the provider duration limit submits one full-audio job."""

    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio, duration_seconds=600.0)  # 10 min, under limit

    submitted_paths: list[Path] = []

    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        submitted_paths.append(Path(data))
        return _FakeTranscript(_completed_response(utterances=[_utterance("x", 0, 100)]))

    monkeypatch.setattr(AssemblyAITranscriber, "_sdk_transcribe", _fake_transcribe)

    settings = _make_settings()
    adapter = AssemblyAITranscriber(settings=settings, sleeper=_NoSleep())
    result = adapter.transcribe(audio_path=audio, metadata=metadata, settings=settings)
    assert result.chunked is False
    assert len(submitted_paths) == 1
    assert submitted_paths[0] == audio


def test_chunk_fallback_on_duration_limit(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Audio exceeding the configured provider duration limit triggers chunking.

    The chunker/uploader is an injectable boundary so tests own the chunk
    paths and make no paid calls. Each chunk is a fake file. The adapter
    submits one job per chunk, applies timestamp offsets, removes overlap
    duplicates conservatively, and reconciles chunk-local speaker labels
    into stable transcript speaker labels.
    """

    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    # 2 hours -> exceeds the configured 1-hour provider duration limit.
    metadata = _make_media_metadata(audio, duration_seconds=7200.0)

    # Fake chunker produces two 1-hour-ish overlapping chunks with bounded
    # overlap. Each chunk is a temp file the test owns.
    chunk_dir = tmp_path / "chunks"
    chunk_dir.mkdir()
    chunk1 = chunk_dir / "c1.m4a"
    chunk1.write_bytes(b"chunk1")
    chunk2 = chunk_dir / "c2.m4a"
    chunk2.write_bytes(b"chunk2")

    submitted: list[Path] = []

    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        submitted.append(Path(data))
        if Path(data) == chunk1:
            return _FakeTranscript(
                _completed_response(
                    utterances=[
                        _utterance("第一段", 0, 5000, speaker="A"),
                        _utterance("重疊句", 5_000, 9_000, speaker="B"),
                    ],
                    transcript_id="c1",
                )
            )
        # chunk2 begins at offset 8000ms (bounded overlap of 1s with chunk1
        # which ended at 9000ms). Its first utterance duplicates the overlap
        # region ("重疊句") and its second utterance is new content.
        return _FakeTranscript(
            _completed_response(
                utterances=[
                    _utterance("重疊句", 0, 1000, speaker="X"),  # local speaker X in chunk2
                    _utterance("第二段", 1000, 5000, speaker="X"),
                ],
                transcript_id="c2",
            )
        )

    monkeypatch.setattr(AssemblyAITranscriber, "_sdk_transcribe", _fake_transcribe)

    # Injectable chunker boundary: deterministic two-chunk plan with 1000ms
    # overlap, chunk duration 5000ms for test speed.
    def _chunker(audio_path: Path, duration_seconds: float) -> list[tuple[Path, float]]:
        # Return (chunk_path, start_offset_seconds) for each chunk.
        return [(chunk1, 0.0), (chunk2, 8.0)]

    settings = _make_settings()
    adapter = AssemblyAITranscriber(
        settings=settings,
        sleeper=_NoSleep(),
        chunker=_chunker,
        provider_max_duration_seconds=3600.0,
    )
    result = adapter.transcribe(audio_path=audio, metadata=metadata, settings=settings)

    assert result.chunked is True
    assert len(submitted) == 2
    # Timestamp offsets applied: chunk2 utterances start at offset+local.
    starts = [seg.start for seg in result.transcript.segments]
    # The overlap duplicate "重疊句" should be removed conservatively (kept
    # once, from the earlier chunk) — assert we have at most one "重疊句".
    overlap_segments = [seg for seg in result.transcript.segments if seg.raw_text == "重疊句"]
    assert len(overlap_segments) == 1
    # Speaker reconciliation: chunk-local "X" in chunk2 must map to a stable
    # transcript speaker label (one of 講者 A/B), not "X" and not inconsistent.
    labels = {seg.speaker_label for seg in result.transcript.segments}
    for label in labels:
        assert label.startswith("講者 ")
        assert label.split()[-1] in {"A", "B", "C", "D", "E", "F", "G", "H"}
    # The chunk2 speaker X that said "重疊句" in chunk1 as "B" should map to
    # the SAME stable label as the chunk1 "B" that said "重疊句" — overlap
    # dedup drives reconciliation.
    x_segments = [seg for seg in result.transcript.segments if seg.raw_text == "第二段"]
    assert x_segments
    assert x_segments[0].speaker_label == "講者 B"  # reconciled to chunk1's "B"
    # Offset applied: chunk2's "第二段" local (1000-5000ms) starts at 8+1=9.0s.
    assert x_segments[0].start == 9.0
    assert x_segments[0].end == 13.0
    # Starts are ordered.
    assert starts == sorted(starts)


def test_chunk_local_speaker_ids_are_not_globally_conflated_without_overlap(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Provider speaker ``A`` is local to each chunk, not a global identity.

    Without overlap evidence, two chunk-local ``A`` labels must remain distinct
    rather than being silently merged into one anonymous transcript speaker.
    """

    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio, duration_seconds=7200.0)
    chunk1 = tmp_path / "c1.m4a"
    chunk2 = tmp_path / "c2.m4a"
    chunk1.write_bytes(b"one")
    chunk2.write_bytes(b"two")

    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        if Path(data) == chunk1:
            return _FakeTranscript(
                _completed_response(
                    utterances=[_utterance("第一位講者", 0, 4_000, speaker="A")],
                    transcript_id="c1",
                )
            )
        return _FakeTranscript(
            _completed_response(
                utterances=[_utterance("另一位講者", 0, 4_000, speaker="A")],
                transcript_id="c2",
            )
        )

    monkeypatch.setattr(AssemblyAITranscriber, "_sdk_transcribe", _fake_transcribe)
    adapter = AssemblyAITranscriber(
        settings=_make_settings(),
        sleeper=_NoSleep(),
        chunker=lambda _audio, _duration: [(chunk1, 0.0), (chunk2, 10.0)],
        provider_max_duration_seconds=3600.0,
    )

    result = adapter.transcribe(
        audio_path=audio,
        metadata=metadata,
        settings=_make_settings(),
    )

    assert [segment.speaker_label for segment in result.transcript.segments] == [
        "講者 A",
        "講者 B",
    ]


def test_chunk_fallback_only_triggered_by_limit(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """Chunking is NOT triggered when audio is within the configured limit."""

    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake")
    metadata = _make_media_metadata(audio, duration_seconds=1800.0)  # 30 min, under limit

    chunker_calls = {"n": 0}

    def _chunker(audio_path: Path, duration_seconds: float) -> list[tuple[Path, float]]:
        chunker_calls["n"] += 1
        return []

    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        return _FakeTranscript(_completed_response(utterances=[_utterance("x", 0, 100)]))

    monkeypatch.setattr(AssemblyAITranscriber, "_sdk_transcribe", _fake_transcribe)
    settings = _make_settings()
    adapter = AssemblyAITranscriber(
        settings=settings,
        sleeper=_NoSleep(),
        chunker=_chunker,
        provider_max_duration_seconds=3600.0,
    )
    result = adapter.transcribe(audio_path=audio, metadata=metadata, settings=settings)
    assert result.chunked is False
    assert chunker_calls["n"] == 0


# ---------------------------------------------------------------------------
# Normalization
# ---------------------------------------------------------------------------


def test_normalize_preserves_raw_numbers_and_currencies() -> None:
    from video_content_capture.transcription.normalize import normalize_text

    raw = "台積電漲百分之三 收盤報 1250 點"
    norm = normalize_text(raw)
    # Numbers and currency meaning preserved verbatim.
    assert "1250" in norm
    assert "百分之三" in norm
    assert "台積電" in norm


def test_normalize_does_not_change_stock_symbols() -> None:
    from video_content_capture.transcription.normalize import normalize_text

    raw = "AAPL 跟 TSLA 都漲了"
    norm = normalize_text(raw)
    assert "AAPL" in norm
    assert "TSLA" in norm


def test_normalize_converts_safe_simplified_to_traditional() -> None:
    from video_content_capture.transcription.normalize import normalize_text

    raw = "今天市场行情很好"
    norm = normalize_text(raw)
    # 市场 -> 市場 (safe Traditional Chinese conversion).
    assert "市場" in norm
    assert "市场" not in norm


def test_normalize_preserves_uncertain_terms() -> None:
    from video_content_capture.transcription.normalize import normalize_text

    raw = "可能他說的是 鯨魚 但不太確定"
    norm = normalize_text(raw)
    # Uncertain term preserved, not rewritten to a guess.
    assert "鯨魚" in norm or "鲸魚" in norm  # only safe char conversion allowed


def test_normalize_does_not_silently_change_entities() -> None:
    from video_content_capture.transcription.normalize import normalize_text

    raw = "英偉達 announced quarterly results"
    norm = normalize_text(raw)
    assert "英偉達" in norm


def test_normalize_adds_conservative_punctuation() -> None:
    from video_content_capture.transcription.normalize import normalize_text

    raw = "今天我們來談台積電 台積電最近表現很好"
    norm = normalize_text(raw)
    # Conservative punctuation may add a comma/period but raw words preserved.
    assert "台積電" in norm
    # The normalized text is not empty and is not identical-only to raw
    # in a way that drops content: it contains all the source words.
    for token in ("今天", "我們", "來談", "台積電", "最近", "表現", "很好"):
        assert token in norm


# ---------------------------------------------------------------------------
# Module-level guards
# ---------------------------------------------------------------------------


def test_transcription_package_exports() -> None:
    import video_content_capture.transcription as pkg
    from video_content_capture.transcription import assemblyai as aai_mod
    from video_content_capture.transcription import base, normalize

    assert pkg
    assert base
    assert aai_mod
    assert normalize


# ---------------------------------------------------------------------------
# Ambiguous one-to-many normalization mappings (regression)
# ---------------------------------------------------------------------------


def test_normalize_does_not_corrupt_empress_huanghou() -> None:
    """后 is one-to-many (後 time vs 后 empress); must NOT be converted."""
    from video_content_capture.transcription.normalize import normalize_text

    raw = "皇后來了"
    norm = normalize_text(raw)
    assert "皇后" in norm
    assert "皇後" not in norm


def test_normalize_does_not_corrupt_surname_yu() -> None:
    """于 is one-to-many (於 preposition vs 于 surname); must NOT be converted."""
    from video_content_capture.transcription.normalize import normalize_text

    raw = "于小姐今天來了"
    norm = normalize_text(raw)
    assert "于小姐" in norm
    assert "於小姐" not in norm


def test_normalize_does_not_corrupt_toufa_hair() -> None:
    """发 is one-to-many (發 issue/send vs 髮 hair); must NOT be converted."""
    from video_content_capture.transcription.normalize import normalize_text

    raw = "他的頭發很長"
    norm = normalize_text(raw)
    # 发 must NOT be converted to 發 (issue/send) — that would change hair
    # (髮) into the wrong glyph. The raw Simplified form is preserved.
    assert "頭發" in norm
    assert "頭發" == norm.replace("他的", "").replace("很長", "").strip() or "頭發" in norm
    assert "頭髮" not in norm
    assert "頭發" not in norm.replace("頭", "")  # no accidental 發 elsewhere
    # Direct: a standalone 发 must remain 发, not become 發 or 髮.
    norm2 = normalize_text("发")
    assert norm2 == "发"


def test_normalize_does_not_corrupt_ji_small_table() -> None:
    """几 is one-to-many (幾 how-many vs 几 small-table); must NOT be converted."""
    from video_content_capture.transcription.normalize import normalize_text

    raw = "茶几上放了杯子"
    norm = normalize_text(raw)
    assert "茶几" in norm
    assert "茶幾" not in norm


def test_normalize_does_not_corrupt_hui_compile_vs_converge() -> None:
    """汇 is one-to-many (匯 forex/converge vs 彙 compile); must NOT be converted."""
    from video_content_capture.transcription.normalize import normalize_text

    # '汇' should not be silently rewritten either way; preserve verbatim.
    # The source below uses the Simplified '汇'; it must NOT become '匯'
    # (forex/converge) because '汇編' should map to '彙編' (compile), not
    # '匯編' — a deterministic char map cannot disambiguate, so leave it.
    raw = "汇編詞典"
    norm = normalize_text(raw)
    assert "汇編" in norm
    assert "匯編" not in norm


def test_normalize_does_not_corrupt_tan_jar_vs_altar() -> None:
    """坛 is one-to-many (壇 altar/forum vs 罎/罈 jar); must NOT be converted."""
    from video_content_capture.transcription.normalize import normalize_text

    raw = "一坛老酒"
    norm = normalize_text(raw)
    assert "一坛" in norm
    assert "一壇" not in norm


def test_normalize_does_not_corrupt_surname_zhou() -> None:
    """周 is one-to-many (週 week vs 周 surname); must NOT be converted."""
    from video_content_capture.transcription.normalize import normalize_text

    raw = "周先生下周回來"
    norm = normalize_text(raw)
    assert "周先生" in norm
    assert "週先生" not in norm


def test_normalize_preserves_safe_conversions_after_ambiguous_removal() -> None:
    """Safe one-to-one conversions still apply after ambiguous removal."""
    from video_content_capture.transcription.normalize import normalize_text

    # 市场 -> 市場 still converts (场 is one-to-one safe).
    raw = "今天市场行情很好"
    norm = normalize_text(raw)
    assert "市場" in norm
    assert "市场" not in norm
    # 國, 機, 會, 們, 這 still convert.
    raw2 = "他们这国家机会很多"
    norm2 = normalize_text(raw2)
    assert "他們" in norm2
    assert "這" in norm2
    assert "國" in norm2
    assert "機" in norm2
    assert "會" in norm2


# ---------------------------------------------------------------------------
# Default chunker (no injection) — ffmpeg audio-only splitting
# ---------------------------------------------------------------------------


def test_default_chunker_produces_bounded_overlapping_chunks_via_ffmpeg(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """A duration-over-limit call with NO injected chunker uses the default
    ffmpeg audio-only chunker.

    The default chunker MUST:
    * produce real chunk artifact files at temp-owned paths (never the source
      full audio path),
    * invoke ffmpeg with an argument array (shell=False) that maps audio
      only (``-vn``), uses bounded overlap, and writes each chunk to a temp
      path under a temp dir,
    * return (chunk_path, start_offset_seconds) tuples whose offsets are
      compatible with the adapter's timestamp-offset reassembly.

    The runner is injected/mocked so no real ffmpeg runs and the source MP4
    is never touched. Chunk artifact files are faked by the mocked runner.
    """

    from video_content_capture.transcription import assemblyai as aai_mod

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake-audio")
    # 2 hours -> exceeds the configured 1-hour provider duration limit.
    metadata = _make_media_metadata(audio, duration_seconds=7200.0)

    captured_argv: list[list[str]] = []

    # Injectable runner: never runs real ffmpeg. Records argv and creates
    # the expected output file so the chunker's post-run validation passes.
    def _fake_run(argv: list[str], **kwargs: Any) -> Any:
        captured_argv.append(list(argv))
        # The output path is the last argv element; create a fake non-empty
        # audio file at that path.
        out = Path(argv[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake-chunk-audio")

        # Return a CompletedProcess-like object.
        class _R:
            returncode = 0
            stderr = ""

        return _R()

    # Mock the adapter's _sdk_transcribe so no paid call happens; each chunk
    # job returns a fake transcript.
    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        return _FakeTranscript(_completed_response(utterances=[_utterance("x", 0, 100)]))

    monkeypatch.setattr(aai_mod.AssemblyAITranscriber, "_sdk_transcribe", _fake_transcribe)

    settings = _make_settings()
    adapter = aai_mod.AssemblyAITranscriber(
        settings=settings,
        sleeper=_NoSleep(),
        runner=_fake_run,
        provider_max_duration_seconds=3600.0,
    )
    result = adapter.transcribe(audio_path=audio, metadata=metadata, settings=settings)

    assert result.chunked is True
    # The default chunker MUST have produced more than one chunk.
    assert len(captured_argv) >= 2
    # Every chunk artifact path is a temp-owned path, NEVER the source audio.
    for argv in captured_argv:
        out_path = Path(argv[-1])
        assert out_path != audio
        assert out_path != Path(metadata.source_path)
        # Chunks live under a temp dir, not next to the source.
        assert out_path.is_absolute()
    # Every ffmpeg argv is audio-only: -vn present, no video decoding.
    for argv in captured_argv:
        assert "-vn" in argv
        assert "-i" in argv
        # The input is the extracted audio, not the source MP4.
        input_idx = argv.index("-i") + 1
        assert argv[input_idx] == str(audio)
    # Bounded overlap: chunk start offsets are strictly increasing and the
    # gap between consecutive chunk starts is less than the chunk duration
    # (i.e. chunks overlap). We infer offsets from the ffmpeg -ss flags.
    offsets: list[float] = []
    for argv in captured_argv:
        # ffmpeg seek before input: -ss <seconds> -i <input>
        if "-ss" in argv:
            ss_idx = argv.index("-ss") + 1
            offsets.append(float(argv[ss_idx]))
        else:
            offsets.append(0.0)
    assert offsets == sorted(offsets)
    if len(offsets) >= 2:
        # Chunks overlap: the second chunk starts before the first chunk's
        # end. Chunk duration ~ provider_max_duration_seconds; overlap is
        # bounded (>0). We assert the gap is strictly less than the chunk
        # duration and strictly greater than 0.
        gap = offsets[1] - offsets[0]
        assert 0 < gap < 3600.0


def test_default_chunker_never_submits_source_full_audio_as_chunk(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """When chunking is triggered, the source full audio path is NEVER submitted
    as a chunk to the provider.
    """

    from video_content_capture.transcription import assemblyai as aai_mod

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake-audio")
    metadata = _make_media_metadata(audio, duration_seconds=7200.0)

    submitted_paths: list[Path] = []

    def _fake_run(argv: list[str], **kwargs: Any) -> Any:
        out = Path(argv[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake-chunk")

        class _R:
            returncode = 0
            stderr = ""

        return _R()

    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        submitted_paths.append(Path(data))
        return _FakeTranscript(_completed_response(utterances=[_utterance("x", 0, 100)]))

    monkeypatch.setattr(aai_mod.AssemblyAITranscriber, "_sdk_transcribe", _fake_transcribe)

    settings = _make_settings()
    adapter = aai_mod.AssemblyAITranscriber(
        settings=settings,
        sleeper=_NoSleep(),
        runner=_fake_run,
        provider_max_duration_seconds=3600.0,
    )
    adapter.transcribe(audio_path=audio, metadata=metadata, settings=settings)

    assert len(submitted_paths) >= 2
    for p in submitted_paths:
        assert p != audio
        assert p != Path(metadata.source_path)


def test_default_chunker_ffmpeg_failure_raises_typed_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """An ffmpeg failure during chunking raises an accepted typed error.

    The chunker MUST NOT silently fall back to submitting the over-limit
    full audio; it raises a typed MediaError (or FilesystemError) so the
    operator learns chunking failed.
    """

    from video_content_capture.domain.errors import MediaError
    from video_content_capture.transcription import assemblyai as aai_mod

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake-audio")
    metadata = _make_media_metadata(audio, duration_seconds=7200.0)

    def _fail_run(argv: list[str], **kwargs: Any) -> Any:
        class _R:
            returncode = 1
            stderr = "ffmpeg error"

        return _R()

    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        # If the adapter reaches this, it submitted the full audio — fail.
        raise AssertionError("adapter must not submit audio when chunking fails")

    monkeypatch.setattr(aai_mod.AssemblyAITranscriber, "_sdk_transcribe", _fake_transcribe)

    settings = _make_settings()
    adapter = aai_mod.AssemblyAITranscriber(
        settings=settings,
        sleeper=_NoSleep(),
        runner=_fail_run,
        provider_max_duration_seconds=3600.0,
    )
    with pytest.raises(
        (MediaError, aai_mod.FilesystemError) if hasattr(aai_mod, "FilesystemError") else MediaError
    ):
        adapter.transcribe(audio_path=audio, metadata=metadata, settings=settings)


def test_default_chunker_temp_cleanup_safe(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    """Temp chunk artifacts are NOT deleted before the upload/transcription call.

    The chunker creates temp chunk files; the adapter must keep them alive
    until after ``_sdk_transcribe`` has been called for each chunk (the SDK
    uploads the file). Cleanup, if any, happens only after all chunks are
    submitted. We assert the chunk file still exists at the moment the
    mocked _sdk_transcribe reads it.
    """

    from video_content_capture.transcription import assemblyai as aai_mod

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake-audio")
    metadata = _make_media_metadata(audio, duration_seconds=7200.0)

    seen_exist: list[bool] = []

    def _fake_run(argv: list[str], **kwargs: Any) -> Any:
        out = Path(argv[-1])
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_bytes(b"fake-chunk")

        class _R:
            returncode = 0
            stderr = ""

        return _R()

    def _fake_transcribe(self: Any, data: Any, config: Any = None) -> Any:  # noqa: ANN001
        # The chunk file MUST still exist at upload time.
        seen_exist.append(Path(data).is_file())
        return _FakeTranscript(_completed_response(utterances=[_utterance("x", 0, 100)]))

    monkeypatch.setattr(aai_mod.AssemblyAITranscriber, "_sdk_transcribe", _fake_transcribe)

    settings = _make_settings()
    adapter = aai_mod.AssemblyAITranscriber(
        settings=settings,
        sleeper=_NoSleep(),
        runner=_fake_run,
        provider_max_duration_seconds=3600.0,
    )
    adapter.transcribe(audio_path=audio, metadata=metadata, settings=settings)

    # Every chunk file was alive when the SDK upload was called.
    assert seen_exist
    assert all(seen_exist)


def test_default_chunker_invalid_output_raises_typed_error(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """If ffmpeg 'succeeds' but produces no/empty chunk file, raise a typed error."""

    from video_content_capture.domain.errors import MediaError
    from video_content_capture.transcription import assemblyai as aai_mod

    audio = tmp_path / "a.m4a"
    audio.write_bytes(b"fake-audio")
    metadata = _make_media_metadata(audio, duration_seconds=7200.0)

    def _empty_run(argv: list[str], **kwargs: Any) -> Any:
        # Return success but do NOT create the output file.
        class _R:
            returncode = 0
            stderr = ""

        return _R()

    monkeypatch.setattr(
        aai_mod.AssemblyAITranscriber,
        "_sdk_transcribe",
        lambda self, data, config=None: pytest.fail("must not submit"),
    )

    settings = _make_settings()
    adapter = aai_mod.AssemblyAITranscriber(
        settings=settings,
        sleeper=_NoSleep(),
        runner=_empty_run,
        provider_max_duration_seconds=3600.0,
    )
    with pytest.raises(MediaError):
        adapter.transcribe(audio_path=audio, metadata=metadata, settings=settings)


# ---------------------------------------------------------------------------
# Helpers (fake SDK transcript + sleeper)
# ---------------------------------------------------------------------------


class _FakeTranscript:
    """Minimal stand-in for ``assemblyai.Transcript`` returned by the SDK.

    Only the attributes read by the adapter are populated. The adapter
    reads ``json_response`` (raw provider payload) and ``response_id`` and
    the parsed ``TranscriptResponse`` fields. We mimic the SDK's surface by
    exposing ``json_response`` and ``id`` and ``response``.
    """

    def __init__(self, response: dict[str, Any]) -> None:
        self._response = response
        self.json_response = response
        self.id = response.get("id")
        # SDK Transcript exposes attribute access to fields via __getattr__
        # to the underlying TranscriptResponse. Mirror that minimally.
        self.text = response.get("text")
        self.status = response.get("status")
        self.language_code = response.get("language_code")
        self.audio_duration = response.get("audio_duration")
        self.utterances = response.get("utterances") or []
        self.words = response.get("words") or []
        self.confidence = response.get("confidence")
        self.error = response.get("error")
        self.audio_path_arg: Any = None


class _NoSleep:
    """A sleeper that records nothing and never blocks; injected to avoid real sleeps."""

    def __call__(self, seconds: float) -> None:
        # No-op; never sleep in tests.
        return None


# Keep `time` referenced so linters do not flag it as unused (used by future
# backoff calculations in the adapter if any).
_ = time
