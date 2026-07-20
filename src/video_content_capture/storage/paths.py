"""Deterministic, readable artifact paths and content/config-keyed cache keys.

Two responsibilities:

* :func:`compute_source_hash` streams the source file in bounded chunks and
  returns its hex SHA-256 digest. The source MP4 may be ~1.545 GB, so the
  whole file MUST NOT be read into memory.
* :func:`compute_config_hash` derives a hex SHA-256 from the effective
  :class:`Settings` fields that affect processing output (language, speaker
  bounds, provider/model settings, retry limits, cleanup behavior). Secrets
  are NEVER serialized into the hash.

Readability: artifact filenames use the source ``<stem>`` (without
extension) so humans can recognize them (``<stem>.transcript.json``).
Determinism: identical source + configuration produces identical cache
locations and output names.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path

from video_content_capture.config import Settings

# Chunk size for streamed hashing. Bounded so the 1.545 GB source is never read
# into memory in one call. 1 MiB is a comfortable default that keeps syscall
# overhead low while guaranteeing a streamed read.
_HASH_CHUNK_BYTES = 1024 * 1024

# Hex SHA-256 digest length (used only for assertions in tests, not for
# truncation).
_SHA256_HEX_LEN = 64


def compute_source_hash(source_path: Path) -> str:
    """Return the hex SHA-256 of ``source_path``'s bytes, read in bounded chunks.

    The file is opened in binary mode and read in :data:`_HASH_CHUNK_BYTES`
    chunks; the full file is never loaded into memory at once. This keeps the
    1.545 GB MP4 streamable.
    """

    hasher = hashlib.sha256()
    # We use the module-level ``open`` so tests can patch it.
    with open(source_path, "rb") as fh:  # noqa: PTH123 — streamed binary read
        while True:
            chunk = fh.read(_HASH_CHUNK_BYTES)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def compute_config_hash(settings: Settings) -> str:
    """Return a hex SHA-256 of the configuration fields that affect output.

    Only fields that change processing results are included. Secrets are
    NEVER serialized: ``assemblyai_api_key`` and ``anthropic_api_key``
    (:class:`pydantic.SecretStr`) are deliberately omitted. Path locations
    (``output_dir``, ``cache_dir``) are also omitted — they control *where*
    artifacts go, not *what* the content is, so they must not change the cache
    identity for a given source + processing configuration.
    """

    # The serializable identity dict. Order is stable (dict insertion order).
    identity: dict[str, object] = {
        "language": settings.language,
        "transcription_backend": settings.transcription_backend,
        "assemblyai_model": settings.assemblyai_model,
        "mlx_whisper_model": settings.mlx_whisper_model,
        "anthropic_model": settings.anthropic_model,
        "min_speakers": settings.min_speakers,
        "max_speakers": settings.max_speakers,
        "max_retries": settings.max_retries,
        "retry_base_delay_seconds": settings.retry_base_delay_seconds,
        "keep_audio": settings.keep_audio,
    }
    payload = json.dumps(identity, sort_keys=True, ensure_ascii=False).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


@dataclass(frozen=True)
class ArtifactPaths:
    """Deterministic, readable output artifact paths for one source."""

    transcript_json: Path
    transcript_md: Path
    report_json: Path
    report_md: Path
    metadata: Path
    manifest: Path
    report_manifest: Path
    raw_dir: Path
    audio: Path


def artifact_paths(*, output_dir: Path, source_path: Path) -> ArtifactPaths:
    """Return deterministic, source-stem-based artifact paths under ``output_dir``.

    Filenames use the source ``<stem>`` (filename without suffix), preserving
    Chinese characters and spaces verbatim so a human can map an artifact back
    to its source file. Deterministic: the same source produces the same names
    on every call.

    Two manifest paths are exposed:

    * ``manifest`` — ``<stem>.manifest.json``, used by the full ``run`` and
      ``transcribe`` commands (which coordinate multiple paid steps).
    * ``report_manifest`` — ``<stem>.report.manifest.json``, used by the
      standalone ``report`` command so it cannot overwrite the full-run
      manifest (a standalone report has its own resume identity keyed on the
      transcript artifact, and must not corrupt the run manifest's
      transcribe+report state).
    """

    stem = source_path.stem
    return ArtifactPaths(
        transcript_json=output_dir / f"{stem}.transcript.json",
        transcript_md=output_dir / f"{stem}.transcript.md",
        report_json=output_dir / f"{stem}.report.json",
        report_md=output_dir / f"{stem}.report.md",
        metadata=output_dir / f"{stem}.metadata.json",
        manifest=output_dir / f"{stem}.manifest.json",
        report_manifest=output_dir / f"{stem}.report.manifest.json",
        raw_dir=output_dir / f"{stem}.raw",
        audio=output_dir / f"{stem}.m4a",
    )


def cache_dir_for(cache_root: Path, *, content_hash: str, config_hash: str) -> Path:
    """Return a deterministic cache directory keyed by content + config hashes.

    The directory combines the two hashes (truncated for filesystem
    friendliness) so a different source OR a different effective configuration
    produces a different cache location. The directory is NOT created here;
    the caller creates it when writing.
    """

    # Truncate each hash to 16 hex chars (8 bytes) — collision-resistant for a
    # single-machine cache while keeping the directory name short and
    # readable.
    return cache_root / f"{content_hash[:16]}-{config_hash[:16]}"


__all__ = [
    "ArtifactPaths",
    "artifact_paths",
    "cache_dir_for",
    "compute_config_hash",
    "compute_source_hash",
]
