"""Deterministic Markdown renderers for canonical artifacts.

The renderers in this package are PURE: they take validated domain models
and project them to Markdown. They contain no provider, SDK, network,
normalization, or LLM logic.
"""

from video_content_capture.markdown.report import (
    render_report_markdown,
    write_report_artifacts,
)
from video_content_capture.markdown.transcript import (
    anchor_for,
    format_timestamp,
    render_transcript_markdown,
    write_transcript_artifacts,
)

__all__ = [
    "anchor_for",
    "format_timestamp",
    "render_report_markdown",
    "render_transcript_markdown",
    "write_report_artifacts",
    "write_transcript_artifacts",
]
