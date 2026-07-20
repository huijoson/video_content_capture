"""Provider-neutral transcription contracts.

This module defines the contracts a managed transcription provider must
implement WITHOUT importing any provider SDK. The domain layer and the
pipeline layer depend on these contracts, never on provider-specific types.

Contracts:

* :class:`Transcriber` — runtime-checkable protocol returning a
  :class:`TranscriptionResult` for one extracted-audio artifact. Implementations
  MUST upload only the extracted audio artifact, never the source MP4.
* :class:`Chunker` — injectable boundary that splits an audio artifact into
  bounded overlapping chunks when a provider limit is exceeded. Chunk audio
  paths and offsets are owned by the caller so tests use owned fixtures and
  never perform paid calls.
* :class:`Sleeper` — injectable backoff delay callable so tests never sleep
  for real.

Provider adapters (e.g. :mod:`video_content_capture.transcription.assemblyai`)
translate provider payloads into provider-neutral
:class:`video_content_capture.domain.models.Transcript` /
:class:`TranscriptSegment` instances, preserving raw provider text and raw
response bytes for audit and recovery.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable

from video_content_capture.config import Settings
from video_content_capture.domain.models import MediaMetadata, Transcript

# --- Result ----------------------------------------------------------------


class TranscriptionResult:
    """Provider-neutral result of transcribing one extracted-audio artifact.

    Attributes:
        transcript: provider-neutral canonical domain transcript.
        raw_text: the raw recognized text returned by the provider, preserved
            separately from the normalized per-segment text.
        raw_response: the raw provider response bytes (JSON or otherwise),
            preserved for audit and recovery. Never contains secrets that
            were not already in the provider payload.
        provider_job_id: the provider's job/transcript identifier, used by the
            manifest to look up a long-running job on resume.
        chunked: ``True`` when this result was assembled from more than one
            provider job (chunk fallback); ``False`` when one full-audio job
            was submitted.
    """

    __slots__ = (
        "transcript",
        "raw_text",
        "raw_response",
        "provider_job_id",
        "chunked",
    )

    def __init__(
        self,
        *,
        transcript: Transcript,
        raw_text: str,
        raw_response: bytes,
        provider_job_id: str,
        chunked: bool,
    ) -> None:
        self.transcript = transcript
        self.raw_text = raw_text
        self.raw_response = raw_response
        self.provider_job_id = provider_job_id
        self.chunked = chunked


# --- Chunker / Sleeper injectable boundaries -------------------------------


#: Type of an injectable chunker: given the audio path and its probed
#: duration in seconds, returns a list of ``(chunk_audio_path, start_offset_seconds)``
#: tuples. The adapter submits one job per chunk and applies the offset to
#: all chunk-local timestamps. Implementations MUST use owned temp paths and
#: avoid paid work; this boundary exists so tests own chunk paths/fixtures.
Chunker = Callable[[Path, float], "list[tuple[Path, float]]"]


#: Type of an injectable sleeper callable used for bounded retry backoff.
#: Tests inject a no-op sleeper so retry tests never block on real time.
Sleeper = Callable[[float], None]


# --- Transcriber protocol --------------------------------------------------


@runtime_checkable
class Transcriber(Protocol):
    """Provider-neutral transcription protocol.

    Implementations transcribe the extracted audio artifact at
    ``audio_path`` and return a :class:`TranscriptionResult`. They MUST:

    * Upload only the extracted audio artifact (``audio_path``), never the
      source MP4 (``metadata.source_path``).
    * Map provider speakers to deterministic anonymous labels such as
      ``講者 A`` and ``講者 B``; never infer personal identities.
    * Preserve raw provider text and raw response bytes separately from the
      normalized per-segment text.
    * Retry only timeouts, HTTP 429, and HTTP 5xx with bounded exponential
      backoff; stop immediately on authentication failure, malformed payload,
      or non-retryable 4xx with accepted typed domain errors.
    * Prefer one full-audio job; use the injected :data:`Chunker` ONLY when a
      configured/verified provider size or duration limit is exceeded.
    """

    def transcribe(
        self,
        *,
        audio_path: Path,
        metadata: MediaMetadata,
        settings: Settings,
    ) -> TranscriptionResult:
        """Transcribe one extracted-audio artifact into a provider-neutral result."""
        ...


__all__ = [
    "Chunker",
    "Sleeper",
    "Transcriber",
    "TranscriptionResult",
]
