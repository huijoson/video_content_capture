"""Grounded plain-language report generation.

Provider-neutral reporting contracts plus the Claude (Anthropic) adapter.

* :mod:`.base` — provider-neutral :class:`Reporter` protocol,
  :class:`ReporterResult`, and evidence-bearing structured response models
  (``StructuredReport`` and per-section models). No provider SDK imports.
* :mod:`.prompts` — builds the model-facing prompt and the ordered structured
  segment payload. Pure, provider-neutral, no SDK imports.
* :mod:`.grounding` — validates a model-returned structured response against
  the canonical transcript BEFORE any renderer/storage write. Rejects unknown
  IDs, empty evidence for source-dependent claims, malformed/missing/
  duplicate required sections, out-of-range referenced segment timing, and
  missing ``講者觀點`` viewpoint labeling.
* :mod:`.claude` — the Claude adapter. The ONLY module that imports the
  Anthropic SDK; no provider SDK types leak into base/prompts/grounding/
  domain/pipeline modules.

All network/SDK calls in the default test suite are mocked at the SDK client
boundary. Live tests, if any, require an explicit ``@pytest.mark.live``
marker and opt-in env (``VCC_ENABLE_LIVE=1``).
"""

from __future__ import annotations

from video_content_capture.reporting import base, grounding, prompts

__all__ = ["base", "grounding", "prompts"]
