"""One catalog for which subtitle version serves each user-facing purpose.

The four selections stay independent (ADR 0002). This module owns the mapping from a
purpose to its column, the recovery of pointers whose version disappeared, the default
original used when a conversation is opened, and the provenance an acquisition job
publishes for later stages.
"""

import sqlite3
from typing import Literal

from video_content_capture.workspace.storage import (
    SUBTITLE_SELECTIONS,
    Library,
    Record,
)

Purpose = Literal["playback", "translation_source", "export", "qa"]
PURPOSES: tuple[Purpose, ...] = ("playback", "translation_source", "export", "qa")
_ORIGINAL_SOURCE_TYPES = frozenset({"platform_manual", "platform_auto", "asr"})


class SubtitleCatalog:
    def __init__(self, library: Library) -> None:
        self.library = library

    def version_for(self, video_id: str, purpose: Purpose) -> Record | None:
        """The selected complete version; a pointer whose row vanished reads as unset."""
        return self._resolve(self.library.get_video(video_id), purpose)

    def recover(self, video_id: str) -> None:
        """Drop stale pointers for every purpose before a caller reads the video row."""
        video = self.library.get_video(video_id)
        for purpose in PURPOSES:
            if video[SUBTITLE_SELECTIONS[purpose]] is not None:
                self._resolve(video, purpose)

    def _resolve(self, video: Record, purpose: Purpose) -> Record | None:
        video_id = str(video["id"])
        selected = video[SUBTITLE_SELECTIONS[purpose]]
        if selected is None:
            return None
        try:
            version = self.library.get_subtitle_version(str(selected))
        except ValueError:
            self.library.clear_subtitle_selection(video_id, purpose, str(selected))
            return None
        if str(version["video_id"]) != video_id or not version["complete"]:
            self.library.clear_subtitle_selection(video_id, purpose, str(selected))
            return None
        return version

    def select(self, video_id: str, purpose: str, version_id: str | None) -> None:
        if purpose not in SUBTITLE_SELECTIONS:
            raise ValueError("Invalid subtitle selection")
        self.library.set_subtitle_selection(video_id, purpose, version_id)

    def default_source(self, video_id: str) -> Record | None:
        """The oldest complete original, used when a conversation is opened implicitly."""
        return next(
            (
                version
                for version in self.library.subtitle_versions(video_id)
                if version["complete"] and str(version["source_type"]) in _ORIGINAL_SOURCE_TYPES
            ),
            None,
        )

    def ensure_conversation(self, video_id: str) -> Record | None:
        source = self.default_source(video_id)
        return self.library.ensure_conversation(
            video_id, str(source["id"]) if source is not None else None
        )

    def record_acquisition(
        self,
        connection: sqlite3.Connection,
        video_id: str,
        job_id: str,
        version_id: str,
    ) -> None:
        """Attribute a published version to its job and adopt it when nothing is pinned."""
        self.library.record_job_result(connection, job_id, {"subtitle_version_id": version_id})
        connection.execute(
            "UPDATE videos SET translation_source_version_id=?, qa_version_id=? "
            "WHERE id=? AND deleting=0 AND translation_source_version_id IS NULL",
            (version_id, version_id, video_id),
        )
