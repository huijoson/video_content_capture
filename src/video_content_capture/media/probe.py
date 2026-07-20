"""Credential-free media probing via ``ffprobe``.

The probe runs ``ffprobe`` with an argument array (``shell=False``) so paths
containing Chinese characters and spaces are preserved exactly without shell
interpolation. Output is parsed into provider-neutral
:class:`video_content_capture.domain.models.MediaMetadata`; no provider SDK
types are imported.

Errors map to the typed domain error :class:`MediaError`:
* a missing source file,
* ``ffprobe`` nonzero exit,
* malformed JSON output,
* a readable media file with no audio stream (transcription would be
  impossible).
"""

from __future__ import annotations

import json
import subprocess
from pathlib import Path
from typing import Any

from video_content_capture.domain.errors import MediaError
from video_content_capture.domain.models import MediaMetadata, StreamInfo

# ffprobe invocation. We request JSON output with both format and stream info.
# ``-v error`` keeps stdout clean of warnings so JSON parsing is reliable.
_FFPROBE_ARGV = (
    "ffprobe",
    "-v",
    "error",
    "-print_format",
    "json",
    "-show_format",
    "-show_streams",
)

# Codec-type values reported by ffprobe that we map to our stream_type enum.
_CODEC_TYPE_TO_STREAM_TYPE: dict[str, str] = {
    "video": "video",
    "audio": "audio",
    "subtitle": "subtitle",
    "data": "data",
    "attachment": "data",
}


def _parse_float(value: Any) -> float | None:
    """Parse a possibly-string numeric ffprobe field into a float."""

    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return float(text)
    except ValueError:
        return None


def _parse_int(value: Any) -> int | None:
    """Parse a possibly-string integer ffprobe field into an int."""

    if value is None:
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(text)
    except ValueError:
        return None


def _stream_language(stream: dict[str, Any]) -> str | None:
    tags = stream.get("tags") or {}
    lang = tags.get("language")
    if lang is None or str(lang) == "und":
        return None
    return str(lang)


def _build_stream_info(stream: dict[str, Any]) -> StreamInfo:
    codec_type = str(stream.get("codec_type") or "")
    stream_type = _CODEC_TYPE_TO_STREAM_TYPE.get(codec_type, "container")
    return StreamInfo(
        index=int(stream["index"]),
        codec=str(stream.get("codec_name") or stream.get("codec_long_name") or ""),
        stream_type=stream_type,  # type: ignore[arg-type]
        language=_stream_language(stream),
        duration_seconds=_parse_float(stream.get("duration")),
        channels=_parse_int(stream.get("channels")),
        sample_rate=_parse_int(stream.get("sample_rate")),
        width=_parse_int(stream.get("width")),
        height=_parse_int(stream.get("height")),
    )


def _build_metadata(source_path: Path, payload: dict[str, Any]) -> MediaMetadata:
    fmt = payload.get("format") or {}
    streams_raw = payload.get("streams") or []
    streams = [_build_stream_info(s) for s in streams_raw]

    # Container duration may be on format or only on individual streams; prefer
    # the format-level duration and fall back to the longest stream duration.
    container_duration = _parse_float(fmt.get("duration"))
    if container_duration is None:
        durations = [s.duration_seconds for s in streams if s.duration_seconds is not None]
        if durations:
            container_duration = max(durations)
        else:
            container_duration = 0.0

    video_streams = [s for s in streams if s.stream_type == "video"]
    audio_streams = [s for s in streams if s.stream_type == "audio"]
    subtitle_streams = [s for s in streams if s.stream_type == "subtitle"]

    return MediaMetadata(
        source_path=str(source_path),
        container=str(fmt.get("format_name") or ""),
        duration_seconds=container_duration,
        video_streams=video_streams,
        audio_streams=audio_streams,
        subtitle_streams=subtitle_streams,
    )


def probe_video(source_path: Path) -> MediaMetadata:
    """Probe a media file and return provider-neutral metadata.

    Runs ``ffprobe`` with an argument array and ``shell=False``. Raises
    :class:`MediaError` for missing files, ``ffprobe`` failures, malformed
    output, or media that contains no audio stream.
    """

    if not source_path.is_file():
        raise MediaError(
            f"source media file not found: {source_path}",
            details={"source_path": str(source_path)},
        )

    argv = [*_FFPROBE_ARGV, str(source_path)]
    try:
        completed = subprocess.run(  # noqa: S603 — argv list, shell=False
            argv,
            shell=False,
            check=False,
            capture_output=True,
            text=True,
        )
    except FileNotFoundError as exc:
        raise MediaError(
            "ffprobe executable was not found on PATH",
            details={"source_path": str(source_path)},
        ) from exc
    except OSError as exc:
        raise MediaError(
            f"failed to execute ffprobe: {exc}",
            details={"source_path": str(source_path)},
        ) from exc

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise MediaError(
            f"ffprobe exited with code {completed.returncode}: {stderr}",
            details={
                "source_path": str(source_path),
                "returncode": completed.returncode,
                "stderr": stderr,
            },
        )

    try:
        payload = json.loads(completed.stdout or "{}")
    except json.JSONDecodeError as exc:
        raise MediaError(
            f"ffprobe returned malformed JSON: {exc}",
            details={
                "source_path": str(source_path),
                "stdout": completed.stdout,
            },
        ) from exc

    metadata = _build_metadata(source_path, payload)
    if not metadata.audio_streams:
        raise MediaError(
            "media file contains no audio stream; transcription is not possible",
            details={
                "source_path": str(source_path),
                "container": metadata.container,
            },
        )
    return metadata


__all__ = ["probe_video"]
