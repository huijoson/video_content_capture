"""Media layer: credential-free probing and audio-only extraction.

This package wraps local ``ffprobe``/``ffmpeg`` subprocesses with argument
arrays (``shell=False``) so Chinese characters and spaces are preserved
exactly, and it returns provider-neutral :class:`MediaMetadata` from the
domain layer. No provider SDK types leak here.
"""

from __future__ import annotations

from video_content_capture.media.audio import extract_audio
from video_content_capture.media.probe import probe_video

__all__ = ["extract_audio", "probe_video"]
