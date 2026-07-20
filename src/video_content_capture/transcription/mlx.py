"""Local Apple-Silicon transcription through MLX Whisper.

The adapter is deliberately small: MLX performs speech recognition locally,
while this module validates and maps its plain-Python result into the existing
provider-neutral transcript contracts. It never uploads media and never
silently falls back to a cloud adapter.
"""

from __future__ import annotations

import importlib
import json
import math
import platform
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast

from video_content_capture.config import Settings
from video_content_capture.domain.errors import ConfigurationError, ProviderPayloadError
from video_content_capture.domain.models import MediaMetadata, Transcript, TranscriptSegment, Word
from video_content_capture.transcription.base import TranscriptionResult
from video_content_capture.transcription.normalize import normalize_text

MLXTranscribeFn = Callable[..., dict[str, Any]]
_TIMING_EPSILON_SECONDS = 0.05


def _load_mlx_transcribe() -> MLXTranscribeFn:
    try:
        module = importlib.import_module("mlx_whisper")
    except (ImportError, OSError) as exc:
        raise ConfigurationError(
            "mlx-whisper is required for local transcription; run `uv sync --dev` "
            "on an Apple Silicon Mac"
        ) from exc

    transcribe = getattr(module, "transcribe", None)
    if not callable(transcribe):
        raise ConfigurationError("installed mlx-whisper does not expose transcribe()")
    return cast("MLXTranscribeFn", transcribe)


def _language_for_mlx(language: str) -> str:
    normalized = language.strip().lower()
    if normalized in {"zh", "zh-tw", "zh-hant", "mandarin", "chinese"}:
        return "zh"
    return normalized.split("-", maxsplit=1)[0]


def _number(value: Any, *, field: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ProviderPayloadError(f"MLX Whisper field {field!r} must be numeric")
    number = float(value)
    if not math.isfinite(number):
        raise ProviderPayloadError(f"MLX Whisper field {field!r} must be finite")
    return number


def _bounded_timing(
    start_value: Any,
    end_value: Any,
    *,
    duration: float,
    field: str,
) -> tuple[float, float]:
    start = _number(start_value, field=f"{field}.start")
    end = _number(end_value, field=f"{field}.end")
    if start < 0 or end < start:
        raise ProviderPayloadError(f"MLX Whisper returned invalid timing for {field}")
    if start > duration + _TIMING_EPSILON_SECONDS or end > duration + _TIMING_EPSILON_SECONDS:
        raise ProviderPayloadError(f"MLX Whisper timing for {field} exceeds media duration")
    return min(start, duration), min(end, duration)


def _map_words(
    raw_words: Any,
    *,
    duration: float,
    segment_index: int,
    segment_start: float,
    segment_end: float,
) -> list[Word]:
    if raw_words is None:
        return []
    if not isinstance(raw_words, list):
        raise ProviderPayloadError("MLX Whisper segment words must be a list")

    words: list[Word] = []
    for word_index, raw_word in enumerate(raw_words):
        if not isinstance(raw_word, dict):
            raise ProviderPayloadError("MLX Whisper word must be an object")
        text_value = raw_word.get("word", raw_word.get("text"))
        if not isinstance(text_value, str) or not text_value.strip():
            raise ProviderPayloadError("MLX Whisper word text must be nonempty")
        start, end = _bounded_timing(
            raw_word.get("start"),
            raw_word.get("end"),
            duration=duration,
            field=f"segments[{segment_index}].words[{word_index}]",
        )
        if (
            start < segment_start - _TIMING_EPSILON_SECONDS
            or end > segment_end + _TIMING_EPSILON_SECONDS
        ):
            raise ProviderPayloadError("MLX Whisper word timing lies outside its segment")
        probability = raw_word.get("probability")
        confidence = None if probability is None else _number(probability, field="probability")
        words.append(
            Word(
                text=text_value,
                start=max(start, segment_start),
                end=min(end, segment_end),
                confidence=confidence,
            )
        )
    words.sort(key=lambda word: word.start)
    return words


def _map_payload(
    payload: Any,
    *,
    metadata: MediaMetadata,
    language: str,
) -> tuple[Transcript, str, bytes]:
    if not isinstance(payload, dict):
        raise ProviderPayloadError("MLX Whisper returned a non-object payload")
    raw_segments = payload.get("segments")
    if not isinstance(raw_segments, list) or not raw_segments:
        raise ProviderPayloadError("MLX Whisper returned no transcript segments")

    duration = metadata.duration_seconds
    segments: list[TranscriptSegment] = []
    for source_index, raw_segment in enumerate(raw_segments):
        if not isinstance(raw_segment, dict):
            raise ProviderPayloadError("MLX Whisper segment must be an object")
        text_value = raw_segment.get("text")
        if not isinstance(text_value, str):
            raise ProviderPayloadError("MLX Whisper segment text must be a string")
        if not text_value.strip():
            # Whisper can emit an empty segment for a silence window in long
            # audio. It carries no transcript evidence, so omit it while
            # preserving the complete raw response for audit.
            continue
        start, end = _bounded_timing(
            raw_segment.get("start"),
            raw_segment.get("end"),
            duration=duration,
            field=f"segments[{source_index}]",
        )
        segments.append(
            TranscriptSegment(
                segment_id="s0000",
                start=start,
                end=end,
                raw_text=text_value,
                normalized_text=normalize_text(text_value.strip()),
                speaker_label="講者 A",
                words=_map_words(
                    raw_segment.get("words"),
                    duration=duration,
                    segment_index=source_index,
                    segment_start=start,
                    segment_end=end,
                ),
            )
        )

    if not segments:
        raise ProviderPayloadError("MLX Whisper returned no usable transcript segments")

    segments.sort(key=lambda segment: segment.start)
    for index, segment in enumerate(segments, start=1):
        segment.segment_id = f"s{index:04d}"

    raw_text_value = payload.get("text")
    raw_text = (
        raw_text_value
        if isinstance(raw_text_value, str) and raw_text_value.strip()
        else " ".join(segment.raw_text for segment in segments)
    )
    try:
        raw_response = json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as exc:
        raise ProviderPayloadError("MLX Whisper payload is not JSON serializable") from exc

    return (
        Transcript(metadata=metadata, segments=segments, language=language),
        raw_text,
        raw_response,
    )


class MLXWhisperTranscriber:
    """On-device MLX Whisper adapter for Apple Silicon."""

    def __init__(
        self,
        *,
        transcribe_fn: MLXTranscribeFn | None = None,
        platform_name: str | None = None,
        machine_name: str | None = None,
    ) -> None:
        current_platform = sys.platform if platform_name is None else platform_name
        current_machine = platform.machine() if machine_name is None else machine_name
        if current_platform != "darwin" or current_machine != "arm64":
            raise ConfigurationError("MLX transcription requires an Apple Silicon Mac")
        self._transcribe = transcribe_fn or _load_mlx_transcribe()

    def transcribe(
        self,
        *,
        audio_path: Path,
        metadata: MediaMetadata,
        settings: Settings,
    ) -> TranscriptionResult:
        try:
            payload = self._transcribe(
                str(audio_path),
                path_or_hf_repo=settings.mlx_whisper_model,
                language=_language_for_mlx(settings.language),
                word_timestamps=True,
                temperature=0.0,
            )
        except (KeyboardInterrupt, ConfigurationError, ProviderPayloadError):
            raise
        except Exception as exc:
            raise ProviderPayloadError(
                f"MLX Whisper inference failed: {type(exc).__name__}"
            ) from exc

        transcript, raw_text, raw_response = _map_payload(
            payload,
            metadata=metadata,
            language=settings.language,
        )
        return TranscriptionResult(
            transcript=transcript,
            raw_text=raw_text,
            raw_response=raw_response,
            provider_job_id=f"local-mlx:{settings.mlx_whisper_model}",
            chunked=False,
        )


__all__ = ["MLXWhisperTranscriber"]
