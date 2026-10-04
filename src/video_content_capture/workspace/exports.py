"""Immutable subtitle export snapshots and verified stream-copy packaging."""

import json
import re
import shutil
import sqlite3
import unicodedata
from collections.abc import Callable
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event
from typing import Annotated, BinaryIO, Literal
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field

from video_content_capture.workspace.jobs import checksum
from video_content_capture.workspace.storage import Library, Record
from video_content_capture.workspace.youtube import SourceError, run_process

ProcessRunner = Callable[[list[str], Event], str]


class ExportTrack(BaseModel):
    model_config = ConfigDict(frozen=True)
    version_id: str
    language: str
    name: str
    srt: str

    @property
    def language_tag(self) -> str:
        # Containers use ISO 639-2; retain regional distinctions in the track title.
        base = self.language.split("-")[0]
        codes = {
            "zh": "zho",
            "en": "eng",
            "ja": "jpn",
            "ko": "kor",
            "es": "spa",
            "fr": "fra",
            "de": "deu",
            "pt": "por",
            "ru": "rus",
            "it": "ita",
            "ar": "ara",
            "hi": "hin",
            "vi": "vie",
            "th": "tha",
            "id": "ind",
            "nl": "nld",
            "sv": "swe",
            "fi": "fin",
            "da": "dan",
            "no": "nor",
            "pl": "pol",
            "cs": "ces",
            "uk": "ukr",
            "tr": "tur",
            "el": "ell",
            "he": "heb",
            "hu": "hun",
            "ro": "ron",
            "bn": "ben",
            "ta": "tam",
            "ms": "msa",
            "tl": "tgl",
            "fa": "fas",
            "ur": "urd",
            "sk": "slk",
            "sl": "slv",
            "bg": "bul",
            "hr": "hrv",
            "sr": "srp",
            "ca": "cat",
            "eu": "eus",
            "gl": "glg",
            "is": "isl",
            "lt": "lit",
            "lv": "lav",
            "et": "est",
            "sw": "swa",
        }
        if base in codes:
            return codes[base]
        if re.fullmatch(r"[a-z]{3}", base):
            return base
        raise SourceError("unsupported_language", "字幕語言無容器標籤對應，請修正語言後重試")

    @property
    def title(self) -> str:
        return f"{self.language} · {self.name} · {self.version_id[:8]}"


def select_tracks(
    targets: list[ExportTrack], original: ExportTrack | None, include_original: bool
) -> list[ExportTrack]:
    selected = list(targets)
    if include_original:
        if original is None:
            raise ValueError("請選擇原文版本")
        selected.append(original)
    unique: dict[str, ExportTrack] = {}
    for track in selected:
        unique.setdefault(track.version_id, track)
    return list(unique.values())


def export_filename(title: str, video_id: str, quality: str, export_id: str, container: str) -> str:
    def clean(value: str, limit: int) -> str:
        safe = "".join(
            char
            for char in value
            if char not in '/\\<>:"|?*' and not unicodedata.category(char).startswith("C")
        )
        safe = re.sub(r"\s+", " ", safe).strip(" .")
        return safe.encode("utf-8")[:limit].decode("utf-8", errors="ignore") or "video"

    if container not in {"mp4", "mkv"}:
        raise ValueError("Invalid export container")
    return (
        f"{clean(title, 80)}-{clean(video_id, 32)}-"
        f"{clean(quality, 40)}-{clean(export_id, 32)}.{container}"
    )


def verified_source(library: Library, video_id: str, asset_id: str) -> tuple[Record, Path]:
    """Resolve a saved media asset through its controlled path and checksum."""
    asset = library.get_asset(asset_id)
    if asset["video_id"] != video_id:
        raise ValueError("影音資產不屬於影片")
    relative = Path(str(asset["path"]))
    with library._directory(relative.parent):
        path = library.root / relative
        if path.is_symlink() or not path.is_file() or checksum(path) != asset["checksum"]:
            raise ValueError("來源影音驗證失敗，請重新取得")
    return asset, path


class MediaExporter:
    def __init__(self, runner: ProcessRunner = run_process) -> None:
        self.runner = runner

    def preview(
        self, source: Path, tracks: list[ExportTrack], directory: Path, cancel: Event
    ) -> str:
        for container in ("mp4", "mkv"):
            try:
                self.mux(source, tracks, container, directory / container, cancel, preview=True)
                return container
            except SourceError as error:
                if cancel.is_set() or error.code == "unsupported_language":
                    raise
        raise SourceError("export_failed", "兩種容器皆無法封裝；來源與字幕已保留")

    def mux(
        self,
        source: Path,
        tracks: list[ExportTrack],
        container: str,
        directory: Path,
        cancel: Event,
        *,
        preview: bool = False,
    ) -> Path:
        if container not in {"mp4", "mkv"}:
            raise ValueError("Invalid export container")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        output = directory / f"{uuid4().hex}.{container}"
        arguments = ["ffmpeg", "-nostdin", "-v", "error", "-i", str(source)]
        for index, track in enumerate(tracks):
            subtitle = directory / f"track-{index}.srt"
            subtitle.write_text(track.srt, encoding="utf-8")
            subtitle.chmod(0o600)
            arguments += ["-i", str(subtitle)]
        arguments += ["-map", "0:v:0", "-map", "0:a:0?", "-c:v", "copy", "-c:a", "copy"]
        for index, track in enumerate(tracks):
            arguments += [
                "-map",
                f"{index + 1}:0",
                f"-metadata:s:s:{index}",
                f"language={track.language_tag}",
                f"-metadata:s:s:{index}",
                f"title={track.title}",
                f"-metadata:s:s:{index}",
                f"handler_name={track.title}",
            ]
        if tracks:
            arguments += ["-c:s", "mov_text" if container == "mp4" else "srt"]
        if preview:
            arguments += ["-t", "0.1"]
        arguments += ["-n", str(output)]
        self.runner(arguments, cancel)
        self.verify(source, output, tracks, cancel)
        return output

    def _streams(self, path: Path, cancel: Event) -> list[dict[str, object]]:
        try:
            info: object = json.loads(
                self.runner(
                    ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(path)], cancel
                )
            )
            if not isinstance(info, dict) or not isinstance(info.get("streams"), list):
                raise ValueError
            streams = info["streams"]
            if not all(isinstance(stream, dict) for stream in streams):
                raise ValueError
            return [dict(stream) for stream in streams]
        except (ValueError, TypeError):
            raise SourceError("invalid_export", "成品軌道驗證失敗") from None

    def verify(self, source: Path, output: Path, tracks: list[ExportTrack], cancel: Event) -> None:
        original = self._streams(source, cancel)
        streams = self._streams(output, cancel)
        subtitles = [s for s in streams if s.get("codec_type") == "subtitle"]
        expected = [(t.language_tag, t.title) for t in tracks]
        actual: list[tuple[object, object]] = []
        for subtitle in subtitles:
            tags = subtitle.get("tags")
            if not isinstance(tags, dict):
                raise SourceError("invalid_export", "成品字幕語言驗證失敗")
            # MP4 exposes the subtitle title as handler_name; MKV uses title.
            actual.append((tags.get("language"), tags.get("title", tags.get("handler_name"))))
        original_av = [
            (s.get("codec_type"), s.get("codec_name"))
            for s in original
            if s.get("codec_type") in {"video", "audio"}
        ]
        output_av = [
            (s.get("codec_type"), s.get("codec_name"))
            for s in streams
            if s.get("codec_type") in {"video", "audio"}
        ]
        # Exactly the selected first video/audio streams, never extra embedded source subtitles.
        selected_av = [next(s for s in original_av if s[0] == "video")]
        selected_av += [s for s in original_av if s[0] == "audio"][:1]
        if (
            actual != expected
            or output_av != selected_av
            or not output.is_file()
            or output.stat().st_size == 0
        ):
            raise SourceError("invalid_export", "成品軌道／語言驗證失敗")


SnapshotId = Annotated[str, Field(min_length=1, max_length=32)]


class SnapshotTrack(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    version_id: SnapshotId
    language: str = Field(min_length=1, max_length=50)
    name: str = Field(min_length=1, max_length=200)


class ExportSnapshot(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    asset_id: SnapshotId
    source_version_id: SnapshotId | None
    target_version_ids: list[SnapshotId] = Field(min_length=1, max_length=1)
    target_languages: list[Annotated[str, Field(min_length=1, max_length=50)]] = Field(
        min_length=1, max_length=1
    )
    include_original: bool
    original_version_id: SnapshotId | None
    container: Literal["mp4", "mkv"]
    tracks: list[SnapshotTrack] = Field(min_length=1, max_length=2)
    title: str = Field(max_length=4096)
    youtube_id: str = Field(pattern=r"^[A-Za-z0-9_-]{11}$")
    quality: str = Field(min_length=1, max_length=1000)


class ExportService:
    def __init__(self, library: Library, exporter: MediaExporter | None = None) -> None:
        self.library = library
        self.exporter = exporter or MediaExporter()

    def _track(self, video_id: str, version_id: str) -> ExportTrack:
        from video_content_capture.workspace.subtitles import render_subtitles

        version = self.library.get_subtitle_version(version_id)
        if version["video_id"] != video_id or not version["complete"]:
            raise ValueError("請選擇本影片的完整字幕版本")
        return ExportTrack(
            version_id=version_id,
            language=str(version["language"]),
            name=str(version["name"]),
            srt=render_subtitles(self.library.subtitle_cues(version_id), "srt").decode("utf-8"),
        )

    def _source(self, video_id: str, asset_id: str) -> tuple[Record, Path]:
        return verified_source(self.library, video_id, asset_id)

    @staticmethod
    def _quality(video: Record, asset: Record) -> str:
        try:
            metadata: object = json.loads(str(video["metadata"]))
            if isinstance(metadata, dict) and isinstance(metadata.get("formats"), list):
                for item in metadata["formats"]:
                    if isinstance(item, dict) and item.get("id") == asset["format_id"]:
                        return f"{item['height']}p-{item['fps']}fps"
        except (ValueError, KeyError, TypeError):
            pass
        return str(asset["format_id"])

    def preview(
        self,
        video_id: str,
        asset_id: str,
        target_version_ids: list[str],
        include_original: bool = False,
        original_version_id: str | None = None,
        source_version_id: str | None = None,
        container: str | None = None,
    ) -> dict[str, object]:
        if len(target_version_ids) != 1:
            raise ValueError("請選擇一份目標字幕版本")
        video = self.library.get_video(video_id)
        if video["deleting"]:
            raise ValueError("影片刪除中")
        asset, source = self._source(video_id, asset_id)
        targets = [self._track(video_id, identifier) for identifier in target_version_ids]
        original = self._track(video_id, original_version_id) if original_version_id else None
        if source_version_id:
            self._track(video_id, source_version_id)
        tracks = select_tracks(targets, original, include_original)
        with TemporaryDirectory(
            prefix=".export-preview-", dir=self.library.video_dir(video_id)
        ) as temp:
            if container is None:
                container = self.exporter.preview(source, tracks, Path(temp), Event())
            else:
                self.exporter.mux(source, tracks, container, Path(temp), Event(), preview=True)
        snapshot = ExportSnapshot(
            asset_id=asset_id,
            source_version_id=source_version_id,
            target_version_ids=[t.version_id for t in targets],
            target_languages=[t.language for t in targets],
            include_original=include_original,
            original_version_id=original_version_id,
            container="mp4" if container == "mp4" else "mkv",
            tracks=[
                SnapshotTrack(version_id=t.version_id, language=t.language, name=t.name)
                for t in tracks
            ],
            title=str(video["title"]),
            youtube_id=str(video["youtube_id"]),
            quality=self._quality(video, asset),
        )
        return snapshot.model_dump()

    def create(self, video_id: str, snapshot: dict[str, object]) -> Record:
        confirmed = ExportSnapshot.model_validate(snapshot)
        verified = self.preview(
            video_id,
            confirmed.asset_id,
            confirmed.target_version_ids,
            confirmed.include_original,
            confirmed.original_version_id,
            confirmed.source_version_id,
            confirmed.container,
        )
        if verified != confirmed.model_dump():
            raise ValueError("匯出摘要已變更，請重新確認格式與字幕軌")
        return self.library.create_snapshot_job(video_id, "export", confirmed.model_dump())

    def run(self, job: Record, attempt: str, cancel: Event) -> None:
        job_id, video_id = str(job["id"]), str(job["video_id"])
        published: list[Path] = []
        try:
            snapshot = ExportSnapshot.model_validate_json(str(job["snapshot"]))
            _, source = self._source(video_id, snapshot.asset_id)
            tracks = [self._track(video_id, t.version_id) for t in snapshot.tracks]
            if [(t.language, t.name) for t in tracks] != [
                (t.language, t.name) for t in snapshot.tracks
            ]:
                raise ValueError("字幕版本內容已變更")
            # Peak additional storage: mux output plus atomic publication copy, and subtitle
            # files plus converted subtitle tracks. Container overhead remains an estimate.
            subtitle_bytes = sum(len(track.srt.encode("utf-8")) for track in tracks)
            required = source.stat().st_size * 2 + subtitle_bytes * 2 + 16 * 1024 * 1024
            if shutil.disk_usage(self.library.root).free < required:
                raise SourceError("insufficient_space", "匯出空間不足，請清理後手動重試")
            self.library.update_attempt(job_id, attempt, "exporting", 0)
            with TemporaryDirectory(
                prefix=".export-", dir=self.library.video_dir(video_id)
            ) as temp:
                directory = Path(temp)
                try:
                    output = self.exporter.mux(
                        source, tracks, snapshot.container, directory, cancel
                    )
                except SourceError:
                    if cancel.is_set():
                        return
                    if snapshot.container == "mp4":
                        # A changed container requires a new confirmed snapshot.
                        self.exporter.mux(
                            source, tracks, "mkv", directory / "fallback", cancel, preview=True
                        )
                        raise SourceError(
                            "container_confirmation_required", "請重新確認 MKV 格式"
                        ) from None
                    raise
                if cancel.is_set():
                    return
                self.library.update_attempt(job_id, attempt, "verifying", 0.9)
                export_id = uuid4().hex
                filename = export_filename(
                    snapshot.title,
                    snapshot.youtube_id,
                    snapshot.quality,
                    export_id,
                    snapshot.container,
                )
                relative = Path("videos") / video_id / "exports" / filename
                with self.library._directory(relative.parent, create=True):
                    pass
                digest = checksum(output)

                def publish(db: sqlite3.Connection) -> None:
                    def write(stream: BinaryIO) -> None:
                        with output.open("rb") as source_stream:
                            shutil.copyfileobj(source_stream, stream)

                    published.append(self.library.publish(relative, write, lambda stream: None))
                    db.execute(
                        "INSERT INTO export_artifacts "
                        "(id,video_id,job_id,path,container,summary,checksum) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (
                            export_id,
                            video_id,
                            job_id,
                            str(relative),
                            snapshot.container,
                            snapshot.model_dump_json(),
                            digest,
                        ),
                    )

                self.library.publish_attempt(job_id, attempt, publish)
        except SourceError as error:
            self.library.update_attempt(job_id, attempt, "failed", error=error.code)
        except Exception:
            for path in published:
                path.unlink(missing_ok=True)
            self.library.update_attempt(job_id, attempt, "failed", error="export_failed")
