"""Audio-only extraction via ``ffmpeg``.

The extractor maps only the selected audio stream, never decodes video frames
(``-vn`` is always present), and writes to a ``.m4a`` artifact.

Strategy:
* For an AAC source audio stream, prefer stream-copy (``-c:a copy``) into
  ``.m4a`` — quality-preserving, no re-encode.
* For any other codec, fall back to a controlled audio-only transcode to AAC
  (``-c:a aac``) into ``.m4a`` — still no video decoding.

After the subprocess completes, the artifact is validated: the file must
exist, be non-empty, and probe as a media file with at least one audio stream.
The source MP4 path is only ever an extraction input (``-i``); it is never
returned as the artifact and never configured as the transcription upload.
"""

from __future__ import annotations

import subprocess
from pathlib import Path

from video_content_capture.domain.errors import MediaError
from video_content_capture.domain.models import MediaMetadata, StreamInfo
from video_content_capture.media.probe import probe_video

# AAC-family codecs eligible for stream-copy into .m4a.
_AAC_CODECS = frozenset({"aac", "aac_latm"})


def _select_audio_stream(metadata: MediaMetadata) -> StreamInfo:
    if not metadata.audio_streams:
        raise MediaError(
            "cannot extract audio: media metadata has no audio stream",
            details={"source_path": metadata.source_path},
        )
    # Use the first audio stream. (Later work could extend this to select by
    # language/channel-count; group 4 keeps the first-stream contract.)
    return metadata.audio_streams[0]


def _build_ffmpeg_argv(
    source_path: str, audio_index: int, codec: str, output_path: Path
) -> list[str]:
    argv: list[str] = [
        "ffmpeg",
        "-hide_banner",
        "-loglevel",
        "error",
        "-y",
        "-i",
        source_path,
        "-vn",  # no video decoding/output — audio-only
        # Map the selected audio stream by its ABSOLUTE container stream index
        # (e.g. ``0:1`` = input 0, stream index 1). ffmpeg's ``0:a:N`` is
        # audio-RELATIVE (N=0 = first audio stream), so for a file with one
        # audio track at absolute index 1, ``0:a:1`` would select a
        # non-existent second audio stream and fail. The absolute form is
        # unambiguous and matches the probed ``StreamInfo.index``.
        "-map",
        f"0:{audio_index}",
    ]
    if codec in _AAC_CODECS:
        argv += ["-c:a", "copy"]
    else:
        argv += ["-c:a", "aac"]
    argv.append(str(output_path))
    return argv


def _validate_artifact(output_path: Path) -> None:
    if not output_path.is_file():
        raise MediaError(
            f"audio extraction artifact was not created: {output_path}",
            details={"output_path": str(output_path)},
        )
    stat = output_path.stat()
    if stat.st_size == 0:
        raise MediaError(
            f"audio extraction artifact is empty: {output_path}",
            details={"output_path": str(output_path)},
        )
    # Probe the artifact to confirm it contains an audio stream. This re-runs
    # ffprobe; it is credential-free and operates on the small cached audio
    # file, never the source MP4.
    try:
        artifact_metadata = probe_video(output_path)
    except MediaError as exc:
        raise MediaError(
            f"extracted audio artifact failed validation: {exc}",
            details={"output_path": str(output_path)},
        ) from exc
    if not artifact_metadata.audio_streams:
        raise MediaError(
            "extracted artifact does not contain an audio stream",
            details={"output_path": str(output_path)},
        )


def extract_audio(metadata: MediaMetadata, output_path: Path) -> Path:
    """Extract the selected audio stream into ``output_path`` and validate it.

    Returns the validated ``.m4a`` artifact path. The source MP4 is only an
    input; it is never returned as the artifact.
    """

    audio = _select_audio_stream(metadata)
    try:
        output_path.parent.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise MediaError(
            f"cannot create audio output directory: {output_path.parent}",
            details={"output_path": str(output_path)},
        ) from exc
    argv = _build_ffmpeg_argv(metadata.source_path, audio.index, audio.codec, output_path)

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
            "ffmpeg executable was not found on PATH",
            details={"output_path": str(output_path)},
        ) from exc
    except OSError as exc:
        raise MediaError(
            f"failed to execute ffmpeg: {exc}",
            details={"output_path": str(output_path)},
        ) from exc

    if completed.returncode != 0:
        stderr = (completed.stderr or "").strip()
        raise MediaError(
            f"ffmpeg exited with code {completed.returncode}: {stderr}",
            details={
                "output_path": str(output_path),
                "returncode": completed.returncode,
                "stderr": stderr,
            },
        )

    _validate_artifact(output_path)
    return output_path


__all__ = ["extract_audio"]
