"""Rebuildable browser previews: at most 720p H.264/AAC faststart MP4, never in place.

A preview is only for in-page watching of sources the browser cannot play. It is published
to the video's own previews directory under a new ID; source assets and export artifacts are
never read for writing, replaced or presented as exports.
"""

import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import BinaryIO, Protocol
from uuid import uuid4

from video_content_capture.workspace.exports import ProcessRunner, verified_source
from video_content_capture.workspace.jobs import checksum
from video_content_capture.workspace.storage import Library, Record
from video_content_capture.workspace.youtube import SourceError, run_process

MAX_PREVIEW_HEIGHT = 720


def moov_first(path: Path) -> bool:
    """True when the top-level moov box precedes mdat (progressive/faststart playback)."""
    try:
        with path.open("rb") as stream:
            while True:
                header = stream.read(8)
                if len(header) < 8:
                    return False
                size, kind = int.from_bytes(header[:4], "big"), header[4:]
                if kind == b"moov":
                    return True
                if kind == b"mdat":
                    return False
                if size == 1:
                    extended = stream.read(8)
                    if len(extended) < 8:
                        return False
                    size, consumed = int.from_bytes(extended, "big"), 16
                else:
                    consumed = 8
                if size < consumed:
                    return False
                stream.seek(size - consumed, 1)
    except OSError:
        return False


class Encoder(Protocol):
    def encode(self, source: Path, directory: Path, cancel: Event) -> Path: ...


def _failed() -> SourceError:
    return SourceError("preview_failed", "相容預覽製作失敗")


class PreviewEncoder:
    """ffmpeg/ffprobe through the injectable, cancellable process boundary."""

    def __init__(self, runner: ProcessRunner = run_process) -> None:
        self.runner = runner

    def _run(self, arguments: list[str], cancel: Event) -> str:
        try:
            return self.runner(arguments, cancel)
        except SourceError:
            if cancel.is_set():
                raise SourceError("cancelled", "工作已取消") from None
            raise _failed() from None

    def _streams(self, path: Path, cancel: Event) -> tuple[list[dict[str, object]], str]:
        output = self._run(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
            cancel,
        )
        try:
            info: object = json.loads(output)
            if not isinstance(info, dict) or not isinstance(info.get("streams"), list):
                raise ValueError
            streams = [dict(item) for item in info["streams"] if isinstance(item, dict)]
            container = info.get("format")
            name = container.get("format_name", "") if isinstance(container, dict) else ""
            return streams, str(name)
        except (ValueError, TypeError):
            raise _failed() from None

    def encode(self, source: Path, directory: Path, cancel: Event) -> Path:
        streams, _ = self._streams(source, cancel)
        video = next((s for s in streams if s.get("codec_type") == "video"), None)
        height = video.get("height") if video else None
        if not isinstance(height, int) or isinstance(height, bool) or height < 2:
            raise _failed()
        # Never upscale; H.264 4:2:0 needs even dimensions.
        target = min(MAX_PREVIEW_HEIGHT, height) // 2 * 2
        audio = any(s.get("codec_type") == "audio" for s in streams)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        output = directory / f"{uuid4().hex}.mp4"
        arguments = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(source)]
        arguments += ["-map", "0:v:0", "-map", "0:a:0?", "-vf", f"scale=-2:{target}"]
        arguments += ["-c:v", "libx264", "-preset", "veryfast", "-crf", "23"]
        arguments += ["-pix_fmt", "yuv420p", "-c:a", "aac", "-b:a", "128k", "-ac", "2"]
        arguments += ["-sn", "-dn", "-map_metadata", "-1", "-movflags", "+faststart"]
        arguments += ["-n", str(output)]
        self._run(arguments, cancel)
        self.verify(output, target, audio, cancel)
        return output

    def verify(self, output: Path, height: int, audio: bool, cancel: Event) -> None:
        streams, container = self._streams(output, cancel)
        video = [s for s in streams if s.get("codec_type") == "video"]
        audio_streams = [s for s in streams if s.get("codec_type") == "audio"]
        if (
            "mp4" not in container.split(",")
            or len(video) != 1
            or video[0].get("codec_name") != "h264"
            or video[0].get("height") != height
            or video[0].get("pix_fmt") != "yuv420p"
            or [s.get("codec_name") for s in audio_streams] != (["aac"] if audio else [])
            or not output.is_file()
            or output.stat().st_size == 0
            or not moov_first(output)
        ):
            raise _failed()


class PreviewService:
    def __init__(self, library: Library, encoder: Encoder | None = None) -> None:
        self.library = library
        self.encoder = encoder or PreviewEncoder()

    def create(self, video_id: str, asset_id: str) -> Record:
        return self.library.create_preview_job(video_id, asset_id)

    def run(self, job: Record, attempt: str, cancel: Event) -> None:
        job_id, video_id = str(job["id"]), str(job["video_id"])
        published: list[Path] = []
        try:
            asset_id = str(json.loads(str(job["snapshot"]))["asset_id"])
            asset, source = verified_source(self.library, video_id, asset_id)
            if asset["browser_playable"]:
                raise SourceError("preview_not_needed", "來源可直接播放")
            # Peak additional storage: encoded output plus its atomic publication copy.
            # A 720p re-encode is usually smaller than the source; this stays an estimate.
            required = source.stat().st_size * 2 + 64 * 1024 * 1024
            if shutil.disk_usage(self.library.root).free < required:
                raise SourceError("insufficient_space", "預覽空間不足，請清理後手動重試")
            self.library.update_attempt(job_id, attempt, "previewing")
            directory = self.library.video_dir(video_id) / "previews"
            with TemporaryDirectory(prefix=".preview-", dir=directory) as temporary:
                output = self.encoder.encode(source, Path(temporary), cancel)
                if cancel.is_set():
                    return
                self.library.update_attempt(job_id, attempt, "verifying")
                preview_id = uuid4().hex
                relative = Path("videos") / video_id / "previews" / f"{preview_id}.mp4"
                digest = checksum(output)
                height = min(MAX_PREVIEW_HEIGHT, self._height(asset))

                def unique(connection: sqlite3.Connection) -> bool:
                    return not cancel.is_set() and (
                        connection.execute(
                            "SELECT 1 FROM media_previews WHERE asset_id=?", (asset_id,)
                        ).fetchone()
                        is None
                    )

                def publish(connection: sqlite3.Connection) -> None:
                    def write(stream: BinaryIO) -> None:
                        with output.open("rb") as source_stream:
                            shutil.copyfileobj(source_stream, stream)

                    published.append(self.library.publish(relative, write, lambda stream: None))
                    connection.execute(
                        "INSERT INTO media_previews (id,video_id,asset_id,job_id,path,checksum,"
                        "height,created_at) VALUES (?,?,?,?,?,?,?,?)",
                        (
                            preview_id,
                            video_id,
                            asset_id,
                            job_id,
                            str(relative),
                            digest,
                            height,
                            datetime.now(UTC).isoformat(),
                        ),
                    )

                self.library.publish_attempt(job_id, attempt, publish, unique)
        except SourceError as error:
            for path in published:
                path.unlink(missing_ok=True)
            self.library.update_attempt(job_id, attempt, "failed", error=error.code)
        except Exception:
            for path in published:
                path.unlink(missing_ok=True)
            self.library.update_attempt(job_id, attempt, "failed", error="preview_failed")

    def _height(self, asset: Record) -> int:
        try:
            video = self.library.get_video(str(asset["video_id"]))
            metadata: object = json.loads(str(video["metadata"]))
            if isinstance(metadata, dict) and isinstance(metadata.get("formats"), list):
                for item in metadata["formats"]:
                    if isinstance(item, dict) and item.get("id") == asset["format_id"]:
                        return int(item["height"])
        except (ValueError, KeyError, TypeError):
            pass
        return MAX_PREVIEW_HEIGHT
