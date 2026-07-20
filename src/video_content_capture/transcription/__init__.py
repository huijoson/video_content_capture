"""Managed Mandarin transcription and diarization.

Provider-neutral transcription contracts plus AssemblyAI and local MLX adapters.

* :mod:`.base` — provider-neutral :class:`Transcriber` protocol and
  :class:`TranscriptionResult`, plus injectable :data:`Chunker` and
  :data:`Sleeper` boundaries.
* :mod:`.assemblyai` — the AssemblyAI adapter. The ONLY module that imports
  the AssemblyAI SDK; no provider SDK types leak into domain/pipeline modules.
* :mod:`.mlx` — the local Apple-Silicon MLX Whisper adapter. MLX is imported
  lazily only when this backend is selected.
* :mod:`.normalize` — conservative Traditional Chinese normalization that
  preserves raw text and never silently changes numbers, currencies,
  entities, stock symbols, or uncertain terms. No LLM.

All network/SDK calls in the default test suite are mocked/injected. Live
tests, if any, require an explicit ``@pytest.mark.live`` marker and opt-in
env (``VCC_ENABLE_LIVE=1``).
"""

from __future__ import annotations

from video_content_capture.transcription import assemblyai, base, normalize

__all__ = ["assemblyai", "base", "normalize"]
