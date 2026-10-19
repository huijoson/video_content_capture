"""Download handler: materialise one selected source format as a published asset."""

import shutil
import sqlite3
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import BinaryIO
from uuid import uuid4

from video_content_capture.workspace.jobs import (
    StageStore,
    checksum,
    fingerprint,
    log_download_failure,
)
from video_content_capture.workspace.storage import Library, Record
from video_content_capture.workspace.youtube import SourceError, SourceMetadata, YoutubeAdapter


class DownloadService:
    """The queue runs this behind the same handler contract as every other stage."""

    def __init__(self, library: Library, adapter: YoutubeAdapter) -> None:
        self.library = library
        self.adapter = adapter

    def run(self, job: Record, attempt: str, cancel: Event) -> None:
        job_id, video_id = str(job["id"]), str(job["video_id"])
        format_id, audio_id = str(job["format_id"]), str(job["audio_id"])
        identity = fingerprint(format_id, audio_id)
        current = ["download"]
        released: list[Path] = []
        try:
            video = self.library.get_video(video_id)
            for asset in self.library.assets(video_id):
                if asset["fingerprint"] != identity:
                    continue
                relative = Path(str(asset["path"]))
                try:
                    with self.library._directory(relative.parent):
                        path = self.library.root / relative
                        reusable = (
                            path.is_file()
                            and not path.is_symlink()
                            and checksum(path) == asset["checksum"]
                        )
                except (ValueError, OSError):
                    reusable = False
                if reusable:
                    if self.library.publish_attempt(
                        job_id,
                        attempt,
                        lambda db: released.extend(self.library.release_stages(db, job_id)),
                    ):
                        self.library.remove_stage_files(released)
                    return
            source = SourceMetadata.model_validate_json(str(video["metadata"]))
            fresh = self.adapter.query(source.source_url, cancel)
            selected_video = next((f for f in fresh.formats if f.id == format_id), None)
            selected_audio = next((a for a in fresh.audio_tracks if a.id == audio_id), None)
            if selected_video is None or selected_audio is None:
                raise SourceError("format_missing", "來源格式消失，請重新載入並選擇")
            # Include temporary streams, checkpoints, merged output and publication copy.
            # Unknown stream sizes make this a lower-bound estimate.
            estimate = sum((f.size or 0) for f in fresh.formats if f.id == format_id)
            estimate += selected_audio.size or 0
            required = max(estimate * 4, 64 * 1024 * 1024)
            if shutil.disk_usage(self.library.root).free < required:
                raise SourceError("insufficient_space", "空間不足，請清理後手動重試")

            def progress(stage: str, done: int | None, total: int | None) -> None:
                fraction = min(done / total, 1) if done is not None and total else None
                current[0] = stage
                self.library.update_attempt(job_id, attempt, stage, fraction)

            with TemporaryDirectory(
                prefix=".attempt-", dir=self.library.video_dir(video_id) / "source"
            ) as temporary:
                media = self.adapter.download(
                    fresh,
                    format_id,
                    audio_id,
                    Path(temporary),
                    cancel,
                    progress,
                    StageStore(self.library, job_id, attempt, video_id),
                )
                if cancel.is_set():
                    return
                if (
                    media.container not in {"mp4", "mkv"}
                    or media.path.is_symlink()
                    or media.path.resolve().parent != Path(temporary).resolve()
                ):
                    raise SourceError("invalid_media", "下載產物驗證失敗")
                asset_id = uuid4().hex
                relative = Path("videos") / video_id / "source" / f"{asset_id}.{media.container}"
                digest = checksum(media.path)
                published: list[Path] = []

                def publish(db: sqlite3.Connection) -> None:
                    def write(stream: BinaryIO) -> None:
                        with media.path.open("rb") as source_file:
                            shutil.copyfileobj(source_file, stream)

                    target = self.library.publish(relative, write, lambda stream: None)
                    published.append(target)
                    db.execute(
                        "INSERT INTO media_assets VALUES(?,?,?,?,?,?,?,?,?)",
                        (
                            asset_id,
                            video_id,
                            format_id,
                            audio_id,
                            str(relative),
                            digest,
                            identity,
                            int(media.browser_playable),
                            media.container,
                        ),
                    )
                    # Stages are kept for retries until the source is published.
                    released.extend(self.library.release_stages(db, job_id))

                try:
                    if self.library.publish_attempt(job_id, attempt, publish):
                        self.library.remove_stage_files(released)
                except Exception:
                    for target in published:
                        target.unlink(missing_ok=True)
                    raise
        except SourceError as error:
            log_download_failure(job_id, current[0], error.code, error)
            self.library.update_attempt(job_id, attempt, "failed", error=error.code)
        except Exception as error:
            log_download_failure(job_id, current[0], "media_failed", error)
            self.library.update_attempt(job_id, attempt, "failed", error="media_failed")
