"""One fenced, atomic publication gate shared by every artifact handler."""

import shutil
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from functools import partial
from pathlib import Path
from typing import BinaryIO

from video_content_capture.workspace.jobs import checksum
from video_content_capture.workspace.storage import Library, Record

RecordPublication = Callable[[sqlite3.Connection, tuple["PublishedArtifact", ...]], Record | None]
OwnerValidator = Callable[[sqlite3.Connection], bool]


@dataclass(frozen=True)
class Artifact:
    relative: Path
    source: Path


@dataclass(frozen=True)
class PublishedArtifact:
    relative: Path
    path: Path
    checksum: str


class ArtifactPublisher:
    """Publish verified files and their owned rows in one fenced transaction.

    Handlers supply a temporary file and the destination path; the gate owns the
    checksum, parent directory, atomic no-replace link, stage release, and cleanup.
    """

    def __init__(self, library: Library) -> None:
        self.library = library

    def servable_path(self, relative: Path) -> Path:
        """Resolve a managed artifact for a read-only route without following links."""
        return self.library.artifact_path(relative)

    def publish(
        self,
        job_id: str,
        attempt: str,
        artifacts: Sequence[Artifact] = (),
        record: RecordPublication | None = None,
        *,
        owner_validator: OwnerValidator | None = None,
        complete: bool = True,
    ) -> bool:
        published: list[PublishedArtifact] = []
        released: list[Path] = []

        def transaction(connection: sqlite3.Connection) -> None:
            for artifact in artifacts:
                digest = checksum(artifact.source)
                with self.library._directory(artifact.relative.parent, create=True):
                    pass
                path = self.library.publish(
                    artifact.relative,
                    partial(self._copy, artifact.source),
                    self._accept,
                )
                published.append(PublishedArtifact(artifact.relative, path, digest))
            if record is not None:
                result = record(connection, tuple(published))
                if result is not None:
                    self.library.record_job_result(connection, job_id, result)
            if complete:
                released.extend(self.library.release_stages(connection, job_id))

        try:
            accepted = self.library.publish_attempt(
                job_id, attempt, transaction, owner_validator, complete=complete
            )
        except Exception:
            self._remove(published)
            raise
        if accepted:
            self.library.remove_stage_files(released)
        else:
            self._remove(published)
        return accepted

    @staticmethod
    def _copy(source: Path, stream: BinaryIO) -> None:
        with source.open("rb") as source_stream:
            shutil.copyfileobj(source_stream, stream)

    @staticmethod
    def _accept(_stream: BinaryIO) -> None:
        return None

    @staticmethod
    def _remove(published: list[PublishedArtifact]) -> None:
        for item in published:
            item.path.unlink(missing_ok=True)
