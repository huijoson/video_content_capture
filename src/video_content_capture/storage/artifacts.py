"""Artifact writers, checksum validation, and success-only audio cleanup.

Each writer serializes a canonical domain model (or raw bytes / text) to a
deterministic path atomically: it writes a temp file in the same directory,
fsyncs, and ``os.replace``'s it onto the final path so an interrupted write
leaves no half-written artifact at the final path.

Checksums: :func:`compute_checksum` returns the hex SHA-256 of an artifact's
bytes. :func:`validate_artifact_checksum` and :func:`validate_completed_step`
verify an artifact exists and matches its recorded checksum BEFORE a resume
reuses a completed step. Mismatch or missing file raises a typed
:class:`FilesystemError` so the caller discards the stale step.

Cleanup: :func:`cleanup_audio` removes the extracted audio artifact ONLY after
a complete run succeeds, and ONLY when ``keep_audio`` is false. It never
touches raw provider payloads, canonical transcript/report JSON, Markdown,
manifest, or run metadata — those remain for audit and resume.
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

from video_content_capture.domain.errors import FilesystemError
from video_content_capture.domain.models import (
    ProcessingStepState,
    Report,
    RunMetadata,
    StepStatus,
    Transcript,
)

# --- atomic write helpers -------------------------------------------------


def _atomic_write_text(path: Path, text: str) -> None:
    """Atomically write ``text`` to ``path`` via temp + fsync + os.replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """Atomically write raw ``data`` to ``path`` via temp + fsync + os.replace."""

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


# --- canonical artifact writers ------------------------------------------


def write_transcript_json(path: Path, transcript: Transcript) -> None:
    """Write canonical transcript JSON atomically."""

    _atomic_write_text(path, transcript.model_dump_json(indent=2))


def write_report_json(path: Path, report: Report) -> None:
    """Write canonical report JSON atomically."""

    _atomic_write_text(path, report.model_dump_json(indent=2))


def write_markdown(path: Path, text: str) -> None:
    """Write a Markdown rendering atomically (no provider/LLM logic here)."""

    _atomic_write_text(path, text)


def write_metadata(path: Path, run: RunMetadata) -> None:
    """Write run metadata JSON atomically."""

    _atomic_write_text(path, run.model_dump_json(indent=2))


def write_raw_provider_payload(raw_dir: Path, *, step_name: str, payload: bytes) -> Path:
    """Write a raw provider response under ``raw_dir`` for audit/recovery.

    The file is named ``<step_name>.json`` so each step's raw payload is
    recognizable. Returns the written path.
    """

    raw_dir.mkdir(parents=True, exist_ok=True)
    path = raw_dir / f"{step_name}.json"
    _atomic_write_bytes(path, payload)
    return path


# --- checksums ------------------------------------------------------------


def compute_checksum(path: Path) -> str:
    """Return the hex SHA-256 of ``path``'s bytes (streamed)."""

    hasher = hashlib.sha256()
    with open(path, "rb") as fh:  # noqa: PTH123 — streamed binary read
        while True:
            chunk = fh.read(1024 * 1024)
            if not chunk:
                break
            hasher.update(chunk)
    return hasher.hexdigest()


def validate_artifact_checksum(path: Path, expected_checksum: str) -> None:
    """Raise :class:`FilesystemError` if ``path`` is missing or checksum mismatches."""

    if not path.is_file():
        raise FilesystemError(
            f"artifact missing or unreadable: {path}",
            details={"path": str(path)},
        )
    actual = compute_checksum(path)
    if actual != expected_checksum:
        raise FilesystemError(
            f"artifact checksum mismatch for {path}: expected {expected_checksum}, got {actual}",
            details={
                "path": str(path),
                "expected": expected_checksum,
                "actual": actual,
            },
        )


def validate_completed_step(
    step: ProcessingStepState,
    *,
    base_dir: Path,
    expected_checksum: str | None = None,
) -> None:
    """Validate a completed step's artifact before trusting it on resume.

    A completed step MUST point at an existing artifact. If
    ``expected_checksum`` is provided (the manifest-side recorded checksum),
    the artifact MUST match it; a mismatch means the artifact was changed or
    corrupted and the step cannot be safely reused.

    ``base_dir`` is reserved for resolving relative ``artifact_path`` values
    in future steps; absolute paths are used directly.
    """

    if step.status is not StepStatus.COMPLETED:
        # Non-completed steps are not subject to checksum validation here.
        return

    artifact_path_str = step.artifact_path
    if not artifact_path_str:
        raise FilesystemError(
            f"completed step {step.step_name!r} has no artifact_path",
            details={"step_name": step.step_name},
        )

    artifact = Path(artifact_path_str)
    # New manifests store absolute paths. For older manifests written with a
    # relative --output-dir, first honor the path relative to the original
    # working directory when it still exists; only then resolve base-relative.
    if not artifact.is_absolute() and not artifact.is_file():
        artifact = base_dir / artifact

    if not artifact.is_file():
        raise FilesystemError(
            f"completed step {step.step_name!r} artifact missing: {artifact}",
            details={"step_name": step.step_name, "path": str(artifact)},
        )

    if expected_checksum is not None:
        validate_artifact_checksum(artifact, expected_checksum)


# --- success-only audio cleanup ------------------------------------------


def cleanup_audio(*, audio_path: Path, keep_audio: bool) -> None:
    """Remove the extracted audio artifact on success unless ``keep_audio``.

    Called ONLY after a complete run succeeds. A missing audio file (e.g. the
    step never ran, or a concurrent cleanup already removed it) is a safe
    no-op: ``FileNotFoundError`` during ``unlink`` is swallowed. Other
    ``OSError`` subclasses (e.g. ``PermissionError`` from a read-only output
    dir or permissions drift) signal an actionable problem and are converted
    to the accepted typed :class:`FilesystemError` so the operator learns the
    audio file was not removed rather than silently retaining large ``.m4a``
    artifacts across many runs. The error carries only the audio path and the
    underlying cause — never file contents or secrets. ``keep_audio=True``
    skips ``unlink`` entirely (does not even attempt it).

    Other artifacts (raw provider payloads, canonical JSON, Markdown,
    manifest, metadata) are NEVER touched here.
    """

    if keep_audio:
        return
    if not audio_path.exists():
        return
    try:
        audio_path.unlink()
    except FileNotFoundError:
        # The audio was already removed (e.g. concurrent cleanup). Safe no-op.
        return
    except OSError as exc:
        # Actionable cleanup failure: surface as the typed FilesystemError so
        # the operator learns the audio was NOT removed. Do not swallow —
        # silently retaining large .m4a files over many runs has real cost.
        raise FilesystemError(
            f"failed to remove extracted audio after successful run: {audio_path}",
            details={"path": str(audio_path)},
        ) from exc


__all__ = [
    "cleanup_audio",
    "compute_checksum",
    "validate_artifact_checksum",
    "validate_completed_step",
    "write_markdown",
    "write_metadata",
    "write_raw_provider_payload",
    "write_report_json",
    "write_transcript_json",
]
