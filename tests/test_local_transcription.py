"""Focused tests for the local MLX Whisper transcription adapter.

All inference is injected. The default suite must not load a model, download
weights, execute ffmpeg, or make a network request.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from video_content_capture.config import Settings
from video_content_capture.domain.errors import ConfigurationError, ProviderPayloadError
from video_content_capture.domain.models import MediaMetadata, StreamInfo


def _metadata(source: Path, *, duration: float = 30.0) -> MediaMetadata:
    return MediaMetadata(
        source_path=str(source),
        container="m4a",
        duration_seconds=duration,
        audio_streams=[
            StreamInfo(
                index=0,
                codec="aac",
                stream_type="audio",
                duration_seconds=duration,
                channels=2,
                sample_rate=44100,
            )
        ],
    )


def _payload() -> dict[str, Any]:
    return {
        "text": "市场上涨 今天继续讨论",
        "language": "zh",
        "segments": [
            {
                "id": 1,
                "start": 4.0,
                "end": 8.5,
                "text": " 今天继续讨论",
                "words": [
                    {"word": "今天", "start": 4.0, "end": 4.6, "probability": 0.94},
                    {"word": "继续", "start": 4.7, "end": 5.3, "probability": 0.91},
                ],
            },
            {
                "id": 0,
                "start": 0.25,
                "end": 3.75,
                "text": "市场上涨",
                "words": [
                    {"word": "上涨", "start": 1.1, "end": 1.8, "probability": 0.96},
                    {"word": "市场", "start": 0.25, "end": 1.0, "probability": 0.98},
                ],
            },
        ],
    }


def test_maps_mlx_payload_to_canonical_transcript(tmp_path: Path) -> None:
    from video_content_capture.transcription.mlx import MLXWhisperTranscriber

    audio = tmp_path / "excerpt.m4a"
    audio.write_bytes(b"local-audio")
    seen: dict[str, Any] = {}

    def fake_transcribe(path: str, **kwargs: Any) -> dict[str, Any]:
        seen["path"] = path
        seen.update(kwargs)
        return _payload()

    settings = Settings(
        transcription_backend="mlx",
        mlx_whisper_model="cached-model",
        language="zh-Hant",
    )
    adapter = MLXWhisperTranscriber(
        transcribe_fn=fake_transcribe,
        platform_name="darwin",
        machine_name="arm64",
    )
    result = adapter.transcribe(
        audio_path=audio,
        metadata=_metadata(tmp_path / "source.mp4"),
        settings=settings,
    )

    assert seen == {
        "path": str(audio),
        "path_or_hf_repo": "cached-model",
        "language": "zh",
        "word_timestamps": True,
        "temperature": 0.0,
    }
    assert result.provider_job_id == "local-mlx:cached-model"
    assert result.chunked is False
    assert result.raw_text == "市场上涨 今天继续讨论"
    assert json.loads(result.raw_response) == _payload()

    transcript = result.transcript
    assert transcript.language == "zh-Hant"
    assert [segment.segment_id for segment in transcript.segments] == ["s0001", "s0002"]
    assert [segment.start for segment in transcript.segments] == [0.25, 4.0]
    assert {segment.speaker_label for segment in transcript.segments} == {"講者 A"}
    assert transcript.segments[0].raw_text == "市场上涨"
    assert transcript.segments[0].normalized_text == "市場上漲"
    assert transcript.segments[0].words[0].text == "市场"
    assert transcript.segments[0].words[0].confidence == 0.98
    assert [word.start for word in transcript.segments[0].words] == [0.25, 1.1]
    assert transcript.segments[1].raw_text == " 今天继续讨论"
    assert transcript.segments[1].normalized_text == "今天继续讨论"


def test_ignores_empty_silence_segment_when_usable_speech_exists(tmp_path: Path) -> None:
    from video_content_capture.transcription.mlx import MLXWhisperTranscriber

    payload = _payload()
    payload["segments"].insert(1, {"start": 3.8, "end": 3.9, "text": "   ", "words": []})
    adapter = MLXWhisperTranscriber(
        transcribe_fn=lambda _path, **_kwargs: payload,
        platform_name="darwin",
        machine_name="arm64",
    )

    result = adapter.transcribe(
        audio_path=tmp_path / "audio.m4a",
        metadata=_metadata(tmp_path / "source.mp4"),
        settings=Settings(transcription_backend="mlx"),
    )

    assert [segment.segment_id for segment in result.transcript.segments] == ["s0001", "s0002"]
    assert all(segment.raw_text.strip() for segment in result.transcript.segments)


@pytest.mark.parametrize(
    "payload",
    [
        {"text": "", "segments": []},
        {"text": "missing segment text", "segments": [{"start": 0.0, "end": 1.0}]},
        {"text": "bad timing", "segments": [{"start": 2.0, "end": 1.0, "text": "x"}]},
        {"text": "nan timing", "segments": [{"start": 0.0, "end": float("nan"), "text": "x"}]},
        {"text": "out of range", "segments": [{"start": 0.0, "end": 31.0, "text": "x"}]},
        {
            "text": "word outside segment",
            "segments": [
                {
                    "start": 0.0,
                    "end": 1.0,
                    "text": "x",
                    "words": [{"word": "x", "start": 0.5, "end": 2.0}],
                }
            ],
        },
    ],
)
def test_rejects_malformed_or_out_of_range_payload(
    tmp_path: Path,
    payload: dict[str, Any],
) -> None:
    from video_content_capture.transcription.mlx import MLXWhisperTranscriber

    adapter = MLXWhisperTranscriber(
        transcribe_fn=lambda _path, **_kwargs: payload,
        platform_name="darwin",
        machine_name="arm64",
    )
    with pytest.raises(ProviderPayloadError):
        adapter.transcribe(
            audio_path=tmp_path / "audio.m4a",
            metadata=_metadata(tmp_path / "source.mp4"),
            settings=Settings(transcription_backend="mlx"),
        )


def test_rejects_unsupported_platform_before_inference() -> None:
    from video_content_capture.transcription.mlx import MLXWhisperTranscriber

    called = False

    def fake_transcribe(_path: str, **_kwargs: Any) -> dict[str, Any]:
        nonlocal called
        called = True
        return _payload()

    with pytest.raises(ConfigurationError, match="Apple Silicon"):
        MLXWhisperTranscriber(
            transcribe_fn=fake_transcribe,
            platform_name="linux",
            machine_name="x86_64",
        )
    assert called is False


@pytest.mark.parametrize("load_error", [ModuleNotFoundError("mlx_whisper"), ImportError("binary")])
def test_missing_mlx_runtime_is_actionable(
    monkeypatch: pytest.MonkeyPatch,
    load_error: ImportError,
) -> None:
    import importlib

    from video_content_capture.transcription.mlx import MLXWhisperTranscriber

    real_import = importlib.import_module

    def missing_runtime(name: str, package: str | None = None) -> Any:
        if name == "mlx_whisper":
            raise load_error
        return real_import(name, package)

    monkeypatch.setattr(importlib, "import_module", missing_runtime)
    with pytest.raises(ConfigurationError, match="mlx-whisper"):
        MLXWhisperTranscriber(platform_name="darwin", machine_name="arm64")
