"""Immutable subtitle export snapshots, verified stream-copy packaging and burned encodes."""

import json
import re
import shutil
import sqlite3
import unicodedata
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Event, Thread
from typing import Annotated, BinaryIO, Literal, Self
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, model_validator

from video_content_capture.workspace.burned import (
    AUDIO_BITRATE,
    SubtitleLayout,
    filter_value,
    render_ass,
    target_bitrate,
)
from video_content_capture.workspace.jobs import checksum
from video_content_capture.workspace.storage import Library, Record
from video_content_capture.workspace.subtitles import Cue
from video_content_capture.workspace.youtube import SourceError, run_process

ProcessRunner = Callable[[list[str], Event], str]
ProgressCallback = Callable[[float | None], None]
# Seconds between reads of ffmpeg's -progress file.
PROGRESS_INTERVAL = 0.25


class ExportTrack(BaseModel):
    model_config = ConfigDict(frozen=True)
    version_id: str
    language: str
    name: str
    srt: str
    cues: tuple[Cue, ...] = ()

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


@dataclass(frozen=True)
class BurnPlan:
    """Probed source properties that fix the burned output's resolution and audio."""

    width: int
    height: int
    duration: float | None
    audio_codec: str | None
    audio_bitrate: int | None

    @property
    def video_bitrate(self) -> int:
        return target_bitrate(self.height)

    def estimated_bytes(self) -> int | None:
        if self.duration is None:
            return None
        audio = 0
        if self.audio_codec == "aac":
            audio = self.audio_bitrate or 320_000
        elif self.audio_codec is not None:
            audio = AUDIO_BITRATE
        return int((self.video_bitrate + audio) * self.duration / 8)


def _hardware_unavailable() -> SourceError:
    return SourceError(
        "hardware_encoder_unavailable",
        "VideoToolbox 硬體編碼不可用，無法燒錄字幕；不會改用軟體編碼或降低畫質",
    )


class MediaExporter:
    def __init__(self, runner: ProcessRunner = run_process) -> None:
        self.runner = runner

    def require_hardware(self, cancel: Event) -> None:
        """Encode a tiny clip with VideoToolbox; never fall back to software encoding."""
        try:
            self.runner(
                [
                    "ffmpeg",
                    "-nostdin",
                    "-v",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=c=black:s=320x240:r=10:d=0.2",
                    "-c:v",
                    "h264_videotoolbox",
                    "-allow_sw",
                    "0",
                    "-f",
                    "null",
                    "-",
                ],
                cancel,
            )
        except SourceError:
            if cancel.is_set():
                raise
            raise _hardware_unavailable() from None

    def _probe(self, path: Path, cancel: Event) -> tuple[list[dict[str, object]], Record]:
        try:
            info: object = json.loads(
                self.runner(
                    [
                        "ffprobe",
                        "-v",
                        "error",
                        "-show_streams",
                        "-show_format",
                        "-of",
                        "json",
                        str(path),
                    ],
                    cancel,
                )
            )
            if not isinstance(info, dict) or not isinstance(info.get("streams"), list):
                raise ValueError
            streams = info["streams"]
            container = info.get("format", {})
            if not all(isinstance(s, dict) for s in streams) or not isinstance(container, dict):
                raise ValueError
            return [dict(s) for s in streams], dict(container)
        except (ValueError, TypeError):
            raise SourceError("invalid_export", "媒體資訊讀取失敗") from None

    def burn_plan(self, source: Path, cancel: Event) -> BurnPlan:
        streams, container = self._probe(source, cancel)
        video = next((s for s in streams if s.get("codec_type") == "video"), {})
        audio = next((s for s in streams if s.get("codec_type") == "audio"), None)
        width, height = video.get("width"), video.get("height")
        if (
            type(width) is not int
            or type(height) is not int
            or width < 2
            or height < 2
            or width % 2
            or height % 2
        ):
            # H.264 4:2:0 needs even dimensions; scaling would change the resolution.
            raise SourceError("invalid_export", "來源影像尺寸無法燒錄")

        def positive(value: object) -> float | None:
            try:
                number = float(str(value))
            except ValueError:
                return None
            return number if 0 < number < float("inf") else None

        bitrate = positive(audio.get("bit_rate")) if audio else None
        return BurnPlan(
            width=width,
            height=height,
            duration=positive(container.get("duration")),
            audio_codec=str(audio.get("codec_name")) if audio else None,
            audio_bitrate=int(bitrate) if bitrate else None,
        )

    def burn(
        self,
        source: Path,
        track: ExportTrack,
        directory: Path,
        cancel: Event,
        progress: ProgressCallback | None = None,
        *,
        preview: bool = False,
        original: ExportTrack | None = None,
    ) -> Path:
        """Draw one subtitle version into the picture: H.264 (VideoToolbox) + AAC MP4."""
        plan = self.burn_plan(source, cancel)
        self.require_hardware(cancel)
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        subtitle = directory / "burned.ass"
        layout = SubtitleLayout(plan.width, plan.height)
        bilingual = (
            (original.cues, original.language)
            # The same version on both sides would only duplicate one line.
            if original is not None and original.version_id != track.version_id
            else None
        )
        subtitle.write_text(
            render_ass(track.cues, track.language, layout, bilingual), encoding="utf-8"
        )
        subtitle.chmod(0o600)
        report = directory / "progress.txt"
        output = directory / f"{uuid4().hex}.mp4"
        arguments = ["ffmpeg", "-nostdin", "-v", "error", "-nostats"]
        arguments += ["-progress", str(report), "-stats_period", str(PROGRESS_INTERVAL)]
        arguments += ["-i", str(source), "-map", "0:v:0", "-map", "0:a:0?"]
        # The subtitles filter draws in source pixels; nothing scales the picture.
        arguments += ["-vf", f"subtitles=filename={filter_value(str(subtitle))}"]
        arguments += ["-c:v", "h264_videotoolbox", "-allow_sw", "0", "-profile:v", "high"]
        arguments += ["-b:v", str(plan.video_bitrate), "-pix_fmt", "yuv420p"]
        if plan.audio_codec == "aac":
            arguments += ["-c:a", "copy"]
        elif plan.audio_codec is not None:
            arguments += ["-c:a", "aac", "-b:a", str(AUDIO_BITRATE)]
        arguments += ["-sn", "-dn", "-movflags", "+faststart"]
        if preview:
            arguments += ["-t", "0.1"]
        arguments += ["-n", str(output)]
        stop = Event()

        def watch() -> None:
            last: float | None = None
            while not stop.wait(PROGRESS_INTERVAL):
                fraction = self._processed(report, plan.duration)
                if progress is not None and fraction is not None and fraction != last:
                    last = fraction
                    progress(fraction)

        watcher = Thread(target=watch, name="workspace-burn-progress", daemon=True)
        if progress is not None:
            # Unknown total duration stays indeterminate instead of a fake percentage.
            progress(0.0 if plan.duration else None)
            watcher.start()
        try:
            self.runner(arguments, cancel)
        except SourceError:
            if cancel.is_set():
                raise
            raise SourceError("burn_failed", "燒錄字幕輸出失敗；來源與字幕已保留") from None
        finally:
            stop.set()
            if watcher.is_alive():
                watcher.join()
        self.verify_burned(output, plan, cancel)
        return output

    @staticmethod
    def _processed(report: Path, duration: float | None) -> float | None:
        """Processed share of the duration from ffmpeg's latest out_time_us."""
        if not duration:
            return None
        try:
            text = report.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return None
        values = re.findall(r"^out_time_us=(\d+)$", text, flags=re.MULTILINE)
        if not values:
            return None
        return min(int(values[-1]) / 1_000_000 / duration, 1.0)

    def verify_burned(self, output: Path, plan: BurnPlan, cancel: Event) -> None:
        streams, container = self._probe(output, cancel)
        video = [s for s in streams if s.get("codec_type") == "video"]
        audio = [s.get("codec_name") for s in streams if s.get("codec_type") == "audio"]
        if (
            "mp4" not in str(container.get("format_name", "")).split(",")
            or len(video) != 1
            or video[0].get("codec_name") != "h264"
            or (video[0].get("width"), video[0].get("height")) != (plan.width, plan.height)
            or audio != (["aac"] if plan.audio_codec is not None else [])
            or any(s.get("codec_type") == "subtitle" for s in streams)
            or not output.is_file()
            or output.stat().st_size == 0
        ):
            raise SourceError("invalid_export", "燒錄成品格式／解析度驗證失敗")

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
SubtitleForm = Literal["burned", "tracks"]


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
    # Snapshots confirmed before burned-in export existed are selectable tracks.
    subtitle_form: SubtitleForm = "tracks"

    @model_validator(mode="after")
    def _burned_shape(self) -> Self:
        if self.subtitle_form == "burned" and (
            # Bilingual burning is one MP4 with the target and original rendered together.
            # `len(self.tracks) > 2` is unreachable while the field caps at two tracks,
            # and stays as a guard should that bound ever widen.
            self.container != "mp4" or len(self.tracks) > 2
        ):
            raise ValueError("Burned exports are one or two languages in MP4")
        return self


class ExportService:
    def __init__(self, library: Library, exporter: MediaExporter | None = None) -> None:
        self.library = library
        self.exporter = exporter or MediaExporter()

    def _track(self, video_id: str, version_id: str) -> ExportTrack:
        from video_content_capture.workspace.subtitles import render_subtitles

        version = self.library.get_subtitle_version(version_id)
        if version["video_id"] != video_id or not version["complete"]:
            raise ValueError("請選擇本影片的完整字幕版本")
        cues = self.library.subtitle_cues(version_id)
        return ExportTrack(
            version_id=version_id,
            language=str(version["language"]),
            name=str(version["name"]),
            srt=render_subtitles(cues, "srt").decode("utf-8"),
            cues=tuple(cues),
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
        subtitle_form: SubtitleForm = "tracks",
    ) -> dict[str, object]:
        if len(target_version_ids) != 1:
            raise ValueError("請選擇一份目標字幕版本")
        if subtitle_form == "burned" and container not in {None, "mp4"}:
            raise ValueError("燒錄字幕成品為 MP4")
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
            if subtitle_form == "burned":
                # A short trial encode proves VideoToolbox and the subtitle render up front.
                self.exporter.burn(
                    source,
                    tracks[0],
                    Path(temp),
                    Event(),
                    preview=True,
                    original=tracks[1] if len(tracks) > 1 else None,
                )
                container = "mp4"
            elif container is None:
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
            subtitle_form=subtitle_form,
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
            confirmed.subtitle_form,
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
            if snapshot.subtitle_form == "burned":
                # Re-encoded output at the target bitrate plus its publication copy.
                estimate = self.exporter.burn_plan(source, cancel).estimated_bytes()
                if estimate is not None:
                    required = estimate * 2 + subtitle_bytes * 2 + 64 * 1024 * 1024
            if shutil.disk_usage(self.library.root).free < required:
                raise SourceError("insufficient_space", "匯出空間不足，請清理後手動重試")
            burned = snapshot.subtitle_form == "burned"
            self.library.update_attempt(job_id, attempt, "burning" if burned else "exporting", 0)
            with TemporaryDirectory(
                prefix=".export-", dir=self.library.video_dir(video_id)
            ) as temp:
                directory = Path(temp)

                def report(fraction: float | None) -> None:
                    self.library.update_attempt(job_id, attempt, "burning", fraction)

                try:
                    if burned:
                        output = self.exporter.burn(
                            source,
                            tracks[0],
                            directory,
                            cancel,
                            report,
                            original=tracks[1] if len(tracks) > 1 else None,
                        )
                    else:
                        output = self.exporter.mux(
                            source, tracks, snapshot.container, directory, cancel
                        )
                except SourceError:
                    if cancel.is_set():
                        return
                    if snapshot.container == "mp4" and not burned:
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
                self.library.update_attempt(job_id, attempt, "verifying", None if burned else 0.9)
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
                        "(id,video_id,job_id,path,container,summary,checksum,subtitle_form) "
                        "VALUES(?,?,?,?,?,?,?,?)",
                        (
                            export_id,
                            video_id,
                            job_id,
                            str(relative),
                            snapshot.container,
                            snapshot.model_dump_json(),
                            digest,
                            snapshot.subtitle_form,
                        ),
                    )

                self.library.publish_attempt(job_id, attempt, publish)
        except SourceError as error:
            self.library.update_attempt(job_id, attempt, "failed", error=error.code)
        except Exception:
            for path in published:
                path.unlink(missing_ok=True)
            self.library.update_attempt(job_id, attempt, "failed", error="export_failed")
