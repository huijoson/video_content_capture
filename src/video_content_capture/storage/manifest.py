"""Typed processing manifest with atomic, interruption-safe persistence.

The manifest records:

* The :class:`RunMetadata` (source path, content/config hashes, attempt id).
* The per-step :class:`ProcessingStepState` map.
* Provider job identifiers (so a long-running cloud job can be looked up
  again on resume).
* Artifact checksums keyed by artifact path (so a completed step's artifact
  can be validated before being trusted on resume).

Writes are atomic: the manifest is serialized to a temporary file in the
same directory, fsync'd, and only then ``os.replace``'d onto the final path.
A simulated interruption (or a real Ctrl-C) that leaves a stale temp file
behind therefore leaves the previous valid manifest at the final path fully
readable.

Reads are interruption-safe: a missing final file yields ``None``; a corrupt
final file also yields ``None`` rather than raising, so a resume attempt can
fail fast with a typed error instead of crashing the CLI. A stale temp file
is never read.
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

from pydantic import BaseModel, ConfigDict, Field

from video_content_capture.domain.errors import ResumeMismatchError
from video_content_capture.domain.models import ProcessingStepState, RunMetadata


class Manifest(BaseModel):
    """Persisted processing state for one attempt."""

    model_config = ConfigDict(extra="forbid")

    run_metadata: RunMetadata
    content_hash: str
    config_hash: str
    attempt_id: str
    steps: dict[str, ProcessingStepState] = Field(default_factory=dict)
    provider_job_ids: dict[str, str] = Field(default_factory=dict)
    artifact_checksums: dict[str, str] = Field(default_factory=dict)


def write_manifest(manifest: Manifest, path: Path) -> None:
    """Serialize ``manifest`` atomically to ``path``.

    Writes a temporary file in the same directory, fsyncs it, then
    ``os.replace``'s it onto ``path``. The previous valid manifest at ``path``
    remains readable right up until the atomic replace.
    """

    path.parent.mkdir(parents=True, exist_ok=True)
    data = manifest.model_dump_json(indent=2)

    # NamedTemporaryFile in the same directory guarantees the os.replace
    # rename stays on the same filesystem (rename across filesystems would
    # raise OSError on POSIX).
    fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=str(path.parent))
    tmp_path = Path(tmp_name)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp_path, path)
    except Exception:
        # Best-effort cleanup of the temp file on any failure; never leave a
        # half-written file behind to be confused with the manifest.
        try:
            if tmp_path.exists():
                tmp_path.unlink()
        except OSError:
            pass
        raise


def read_manifest(path: Path) -> Manifest | None:
    """Read a manifest from ``path``, returning ``None`` if absent or corrupt.

    Interruption-safe: a missing or corrupt final manifest yields ``None``
    so a caller can surface a typed error rather than crashing. A stale
    temp file is never read.
    """

    if not path.is_file():
        return None
    try:
        raw = path.read_text(encoding="utf-8")
    except OSError:
        return None
    try:
        return Manifest.model_validate_json(raw)
    except Exception:
        # Corrupt or partial final file: treat as unreadable.
        return None


def load_resume_state(
    path: Path, *, content_hash: str, config_hash: str, force: bool = False
) -> Manifest | None:
    """Load prior manifest state, validating compatibility for resume.

    Returns ``None`` when there is no manifest, when ``force=True`` (start a
    new attempt regardless of compatibility), or implicitly when the
    manifest is missing/corrupt.

    Raises :class:`ResumeMismatchError` when a manifest exists but the
    content hash or config hash differs from the current run — the caller
    must start a new attempt (e.g. via ``--force``) rather than silently
    reusing incompatible paid results.
    """

    if force:
        # --force always begins a fresh attempt; never reuse prior state.
        return None

    manifest = read_manifest(path)
    if manifest is None:
        return None

    if manifest.content_hash != content_hash or manifest.config_hash != config_hash:
        raise ResumeMismatchError(
            "Stored manifest is incompatible with the current run; start a new "
            "attempt with --force to discard the previous state.",
            details={
                "content_hash": {
                    "stored": manifest.content_hash,
                    "current": content_hash,
                },
                "config_hash": {
                    "stored": manifest.config_hash,
                    "current": config_hash,
                },
            },
        )
    return manifest


__all__ = ["Manifest", "load_resume_state", "read_manifest", "write_manifest"]
