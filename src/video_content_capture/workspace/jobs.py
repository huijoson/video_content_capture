"""Single media worker with explicit retries and guarded publication."""

import hashlib
import logging
import shutil
import sqlite3
import threading
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import BinaryIO
from uuid import uuid4

from video_content_capture.redaction import scrub_text
from video_content_capture.workspace.storage import Library, Record
from video_content_capture.workspace.youtube import (
    DownloadStages,
    SourceError,
    SourceMetadata,
    YoutubeAdapter,
)

logger = logging.getLogger("vcc.workspace")
FAILURE_MESSAGE_LIMIT = 300


def log_download_failure(job_id: str, stage: str, code: str, error: Exception) -> None:
    """One redacted diagnostic line; never keys, subtitle text, questions or answers."""
    message = " ".join(scrub_text(str(error)).split())[:FAILURE_MESSAGE_LIMIT]
    logger.warning(
        "Download failure job=%s stage=%s code=%s error=%s message=%s",
        job_id,
        stage,
        code,
        type(error).__name__,
        message,
    )


def checksum(path: Path) -> str:
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def fingerprint(format_id: str, audio_id: str) -> str:
    return hashlib.sha256(f"{format_id}\n{audio_id}\nstream-copy-v1".encode()).hexdigest()


class StageStore(DownloadStages):
    def __init__(self, library: Library, job_id: str, attempt: str, video_id: str) -> None:
        self.library, self.job_id, self.attempt, self.video_id = library, job_id, attempt, video_id

    def load(self, name: str, format_id: str) -> Path | None:
        identity = fingerprint(name, format_id)
        for stage in self.library.stage_records(self.job_id):
            if stage["name"] == name and stage["fingerprint"] == identity:
                relative = Path(str(stage["path"]))
                try:
                    with self.library._directory(relative.parent):
                        path = self.library.root / relative
                        if (
                            not path.is_symlink()
                            and path.is_file()
                            and checksum(path) == stage["checksum"]
                        ):
                            return path
                except (ValueError, OSError):
                    pass
        return None

    def save(self, name: str, format_id: str, path: Path) -> None:
        relative = Path("videos") / self.video_id / "source" / f"stage-{uuid4().hex}.bin"
        digest = checksum(path)
        published: list[Path] = []

        def publish(db: sqlite3.Connection) -> None:
            def write(stream: BinaryIO) -> None:
                with path.open("rb") as source:
                    shutil.copyfileobj(source, stream)

            published.append(self.library.publish(relative, write, lambda stream: None))

        try:
            accepted = self.library.publish_stage(
                self.job_id,
                self.attempt,
                name,
                str(relative),
                digest,
                fingerprint(name, format_id),
                publish,
            )
        except Exception:
            for target in published:
                target.unlink(missing_ok=True)
            raise
        if not accepted:
            raise SourceError("cancelled", "階段結果已失效")


class MediaQueue:
    def __init__(
        self,
        library: Library,
        adapter: YoutubeAdapter,
        handlers: dict[str, Callable[[Record, str, threading.Event], None]] | None = None,
        name: str = "workspace-media",
    ) -> None:
        self.library = library
        self.adapter = adapter
        self.handlers = handlers or {}
        self.condition = threading.Condition()
        self.pending: list[str] = []
        self.active: dict[str, threading.Event] = {}
        self.stopping = False
        self.on_finished: Callable[[str], None] | None = None
        self.thread = threading.Thread(target=self._worker, name=name, daemon=True)

    def start(self) -> None:
        self.thread.start()

    def close(self) -> None:
        with self.condition:
            self.stopping = True
            self.library.interrupt_running()
            for event in self.active.values():
                event.set()
            self.condition.notify_all()
        self.thread.join(timeout=15)

    def enqueue(self, job: Record) -> None:
        job_id = str(job["id"])
        with self.condition:
            if job["status"] == "queued" and job_id not in self.pending:
                self.pending.append(job_id)
                self.condition.notify()

    def cancel(self, job_id: str) -> None:
        self.library.cancel_job(job_id)
        with self.condition:
            queued = job_id in self.pending
            if queued:
                self.pending.remove(job_id)
            if job_id in self.active:
                self.active[job_id].set()
        # A job cancelled while still queued never reaches the worker, so the completion
        # hook in `_worker` would never run for it. Flow chaining depends on that hook to
        # end the flow when a stage does not complete, so fire it here instead.
        if queued:
            self._finished(job_id)

    def signal(self, job_id: str) -> None:
        """Stop a job whose DB status was already changed by a cleanup transaction."""
        with self.condition:
            if job_id in self.pending:
                self.pending.remove(job_id)
            if job_id in self.active:
                self.active[job_id].set()

    def wait_stopped(self, job_ids: list[str], timeout: float) -> bool:
        """Wait until none of the jobs is still running in this worker."""
        with self.condition:
            return self.condition.wait_for(
                lambda: not any(job_id in self.active for job_id in job_ids), timeout
            )

    def _worker(self) -> None:
        while True:
            with self.condition:
                self.condition.wait_for(lambda: self.stopping or bool(self.pending))
                if self.stopping:
                    return
                job_id = self.pending.pop(0)
                event = threading.Event()
                self.active[job_id] = event
            try:
                self.process(job_id, event)
            finally:
                self._finished(job_id)
                with self.condition:
                    self.active.pop(job_id, None)
                    self.condition.notify_all()

    def _finished(self, job_id: str) -> None:
        # The completion hook chains the next flow stage; it must never stop the lane.
        if self.on_finished is None:
            return
        try:
            self.on_finished(job_id)
        except Exception:
            logger.exception("Job completion hook failed job=%s", job_id)

    def process(self, job_id: str, cancel: threading.Event) -> None:
        try:
            attempt = self.library.start_job(job_id)
        except ValueError:
            return
        job = self.library.get_job(job_id)
        if job["kind"] != "download":
            try:
                handler = self.handlers[str(job["kind"])]
                self.library.update_attempt(job_id, attempt, str(job["kind"]))
                handler(job, attempt, cancel)
            except SourceError as error:
                self.library.update_attempt(job_id, attempt, "failed", error=error.code)
            except Exception:
                self.library.update_attempt(job_id, attempt, "failed", error="processing_failed")
            return
        video_id, format_id, audio_id = (
            str(job[key]) for key in ("video_id", "format_id", "audio_id")
        )
        identity = fingerprint(format_id, audio_id)
        current = ["download"]
        released: list[Path] = []
        try:
            video = self.library.get_video(video_id)
            for asset in self.library.assets(video_id):
                if asset["fingerprint"] == identity:
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


class JobLanes:
    """Route QA to its own worker; media, subtitle, translation and export stay serial."""

    def __init__(self, library: Library, media: MediaQueue, qa: MediaQueue) -> None:
        self.library, self.media, self.qa = library, media, qa

    def _lane(self, job: Record) -> MediaQueue:
        return self.qa if job["kind"] == "qa" else self.media

    def start(self) -> None:
        self.media.start()
        self.qa.start()

    def close(self) -> None:
        self.qa.close()
        self.media.close()

    def enqueue(self, job: Record) -> None:
        self._lane(job).enqueue(job)

    def bind(self, on_finished: Callable[[str], None]) -> None:
        """Flow chaining runs on the media lane's completion hook, outside the serial worker."""
        self.media.on_finished = on_finished

    def cancel(self, job_id: str) -> None:
        self._lane(self.library.get_job(job_id)).cancel(job_id)

    def signal(self, job_ids: list[str]) -> None:
        for job_id in job_ids:
            self.media.signal(job_id)
            self.qa.signal(job_id)

    def wait_media_stopped(self, job_ids: list[str], timeout: float) -> bool:
        """Only media-lane workers write managed files; QA replies are fenced by the gate."""
        return self.media.wait_stopped(job_ids, timeout)
