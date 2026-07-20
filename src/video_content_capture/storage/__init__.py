"""Storage layer: deterministic paths, atomic manifest, and resume.

Provider-neutral artifact storage for the Video Content Capture pipeline.

* :mod:`.paths` — streamed source SHA-256, configuration-sensitive cache
  keys (no secrets), and readable deterministic source-based output names.
* :mod:`.manifest` — typed step state, atomic temp-file + ``os.replace``
  manifest writes, interruption-safe reads, provider job identifiers, and
  artifact checksums.
* :mod:`.artifacts` — writers for canonical transcript/report JSON, Markdown,
  metadata, and raw provider payloads, plus checksum validation and
  success-only audio cleanup.

No CLI, provider, or renderer logic lives here.
"""

from __future__ import annotations

from video_content_capture.storage import artifacts, manifest, paths

__all__ = ["artifacts", "manifest", "paths"]
