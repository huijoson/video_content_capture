"""Single-video source boundary with cancellable, isolated yt-dlp Python API calls."""

import json
import math
import os
import re
import signal
import subprocess
import sys
from collections.abc import Callable
from fractions import Fraction
from pathlib import Path
from threading import Event
from typing import Protocol
from urllib.parse import parse_qs, urlsplit

from pydantic import BaseModel, ConfigDict

from video_content_capture.redaction import scrub_text

Progress = Callable[[str, int | None, int | None], None]
_ID = re.compile(r"[A-Za-z0-9_-]{11}\Z")
_FORMAT_ID = re.compile(r"[A-Za-z0-9_.-]+\Z")
_HOSTS = {"youtube.com", "www.youtube.com", "m.youtube.com", "music.youtube.com"}


class SourceError(Exception):
    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code


class VideoFormat(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    height: int
    fps: float
    codec: str
    size: int | None = None


class AudioTrack(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    language: str
    codec: str
    original: bool
    default: bool
    size: int | None = None


class SubtitleTrack(BaseModel):
    model_config = ConfigDict(frozen=True)
    language: str
    automatic: bool
    extensions: list[str]


class SourceMetadata(BaseModel):
    model_config = ConfigDict(frozen=True)
    youtube_id: str
    source_url: str
    title: str
    duration: float
    formats: list[VideoFormat]
    audio_tracks: list[AudioTrack]
    subtitles: list[SubtitleTrack]
    default_format_id: str
    default_audio_id: str
    above_1080p: bool
    original_language: str | None


class DownloadedMedia(BaseModel):
    model_config = ConfigDict(frozen=True)
    path: Path
    container: str
    video_codec: str
    audio_codec: str
    browser_playable: bool


class DownloadStages(Protocol):
    """Verified checkpoints owned by the queue's transaction and checksum boundary."""

    def load(self, name: str, format_id: str) -> Path | None: ...

    def save(self, name: str, format_id: str, path: Path) -> None: ...


class YoutubeAdapter(Protocol):
    def query(self, url: str, cancel: Event | None = None) -> SourceMetadata: ...

    def download_subtitle(
        self, source: SourceMetadata, track: SubtitleTrack, cancel: Event
    ) -> bytes: ...

    def download(
        self,
        source: SourceMetadata,
        format_id: str,
        audio_id: str,
        directory: Path,
        cancel: Event,
        progress: Progress,
        stages: DownloadStages | None = None,
    ) -> DownloadedMedia: ...


def parse_youtube_url(url: str) -> tuple[str, str]:
    """Drop all unneeded URL data; never accept credentials or extractor redirects."""
    invalid = SourceError("invalid_url", "請輸入單支 YouTube 影片網址")
    if not url or any(character.isspace() for character in url):
        raise invalid
    try:
        parsed = urlsplit(url)
        if (
            parsed.scheme not in {"https", "http"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed.port not in {None, 80, 443}
        ):
            raise invalid
        host = parsed.hostname
        if host in {"youtu.be", "www.youtu.be"}:
            video_id = parsed.path.removeprefix("/")
        elif host in _HOSTS:
            if parsed.path == "/watch":
                values = parse_qs(parsed.query).get("v", [])
                if len(values) != 1:
                    raise invalid
                video_id = values[0]
            else:
                parts = parsed.path.split("/")
                if len(parts) != 3 or parts[1] not in {"shorts", "embed", "live"}:
                    raise invalid
                video_id = parts[2]
        else:
            raise invalid
    except ValueError:
        raise invalid from None
    if _ID.fullmatch(video_id) is None:
        raise invalid
    return video_id, f"https://www.youtube.com/watch?v={video_id}"


def _number(value: object) -> float | None:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        number = float(value)
        if math.isfinite(number) and number >= 0:
            return number
    return None


def _size(item: dict[str, object]) -> int | None:
    number = _number(item.get("filesize")) or _number(item.get("filesize_approx"))
    return int(number) if number else None


def _string(value: object, fallback: str = "") -> str:
    return scrub_text(value) if isinstance(value, str) else fallback


def parse_metadata(info: dict[str, object], url: str) -> SourceMetadata:
    video_id, canonical = parse_youtube_url(url)
    unavailable = SourceError("source_unavailable", "來源不支援／無法取得")
    if (
        info.get("id") != video_id
        or info.get("is_live") is True
        or info.get("live_status") in {"is_live", "is_upcoming", "post_live"}
        or info.get("availability") in {"private", "needs_auth", "premium_only", "subscriber_only"}
        or info.get("_type") in {"playlist", "multi_video"}
    ):
        raise unavailable
    duration = _number(info.get("duration"))
    raw_formats = info.get("formats")
    if duration is None or duration <= 0 or not isinstance(raw_formats, list):
        raise unavailable
    language = _string(info.get("language")) or None
    formats: list[VideoFormat] = []
    tracks: list[AudioTrack] = []
    for raw in raw_formats:
        if not isinstance(raw, dict):
            continue
        item: dict[str, object] = raw
        identifier = _string(item.get("format_id"))
        if _FORMAT_ID.fullmatch(identifier) is None or item.get("has_drm") is True:
            continue
        video_codec = _string(item.get("vcodec"), "none")
        audio_codec = _string(item.get("acodec"), "none")
        height = _number(item.get("height"))
        if video_codec != "none" and height and height.is_integer():
            formats.append(
                VideoFormat(
                    id=identifier,
                    height=int(height),
                    fps=_number(item.get("fps")) or 0,
                    codec=video_codec,
                    size=_size(item),
                )
            )
        if audio_codec != "none":
            track_language = _string(item.get("language")) or "未知"
            note = _string(item.get("format_note")).lower()
            original = "original" in note and "dub" not in note
            default = "default" in note or (_number(item.get("language_preference")) or 0) > 0
            tracks.append(
                AudioTrack(
                    id=identifier,
                    language=track_language,
                    codec=audio_codec,
                    original=original,
                    default=default,
                    size=_size(item),
                )
            )
    if not formats or not tracks:
        raise unavailable
    below = [item for item in formats if item.height <= 1080]
    target_height = (
        max(item.height for item in below) if below else min(item.height for item in formats)
    )
    # The extractor lists formats worst to best; final tie keeps its last entry.
    candidates = [item for item in formats if item.height == target_height]
    selected = max(enumerate(candidates), key=lambda pair: (pair[1].fps, pair[0]))[1]
    original_tracks = [track for track in tracks if track.original]
    selected_audio = next((track for track in reversed(original_tracks) if track.default), None)
    if selected_audio is None:
        selected_audio = (original_tracks or tracks)[-1]
    subtitles: list[SubtitleTrack] = []
    for key, automatic in (("subtitles", False), ("automatic_captions", True)):
        raw_subtitles = info.get(key)
        if not isinstance(raw_subtitles, dict):
            continue
        for subtitle_language, entries in raw_subtitles.items():
            if not isinstance(subtitle_language, str) or not isinstance(entries, list):
                continue
            extensions = sorted(
                {
                    scrub_text(entry["ext"])
                    for entry in entries
                    if isinstance(entry, dict) and isinstance(entry.get("ext"), str)
                }
            )
            subtitles.append(
                SubtitleTrack(
                    language=scrub_text(subtitle_language),
                    automatic=automatic,
                    extensions=extensions,
                )
            )
    if language is None and selected_audio.original and selected_audio.language != "未知":
        language = selected_audio.language
    return SourceMetadata(
        youtube_id=video_id,
        source_url=canonical,
        title=_string(info.get("title"), video_id),
        duration=duration,
        formats=formats,
        audio_tracks=tracks,
        subtitles=subtitles,
        default_format_id=selected.id,
        default_audio_id=selected_audio.id,
        above_1080p=not below,
        original_language=language,
    )


# Python API is isolated so cancellation can terminate every descendant, including ffmpeg.
_HELPER = r"""
import json, sys
import yt_dlp
class Quiet:
    def debug(self, message): pass
    def warning(self, message): pass
    def error(self, message): pass
options = dict(quiet=True, no_warnings=True, logger=Quiet(), noplaylist=True,
               retries=0, fragment_retries=0, extractor_retries=0, socket_timeout=20,
               cachedir=False, writeinfojson=False, writesubtitles=False,
               writeautomaticsub=False)
request = json.loads(sys.stdin.read())
if request['mode'] == 'subtitle':
    options.update(skip_download=True, writesubtitles=not request['automatic'],
                   writeautomaticsub=request['automatic'], subtitleslangs=[request['language']],
                   subtitlesformat='vtt', outtmpl=request['output'])
if request['mode'] == 'download':
    options.update(format=request['format'], outtmpl=request['output'],
                   fixup='never', postprocessors=[])
try:
    with yt_dlp.YoutubeDL(options) as downloader:
        info = downloader.extract_info(
            request['url'], download=request['mode'] in ('download', 'subtitle'))
        if request['mode'] == 'subtitle':
            print(json.dumps({'ok': True}))
        elif request['mode'] == 'query':
            print(json.dumps(downloader.sanitize_info(info)))
        else:
            print(json.dumps({'path': downloader.prepare_filename(info)}))
except Exception:
    sys.exit(1)
"""


def run_process(command: list[str], cancel: Event, *, request: str | None = None) -> str:
    """Bounded polling plus group termination; no stderr details escape this boundary."""
    if cancel.is_set():
        raise SourceError("cancelled", "工作已取消")
    try:
        process = subprocess.Popen(
            command,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            start_new_session=True,
        )
    except OSError:
        raise SourceError("tool_unavailable", "媒體工具無法啟動") from None
    pending_input = request
    try:
        while True:
            if cancel.is_set():
                raise SourceError("cancelled", "工作已取消")
            try:
                output, _ = process.communicate(input=pending_input, timeout=0.1)
                break
            except subprocess.TimeoutExpired:
                pending_input = None
        if process.returncode != 0:
            raise SourceError("source_unavailable", "來源不支援／無法取得，請手動重試")
        return output
    finally:
        if process.poll() is None:
            try:
                os.killpg(process.pid, signal.SIGTERM)
            except ProcessLookupError:
                pass
            try:
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(process.pid, signal.SIGKILL)
                except ProcessLookupError:
                    pass
                process.wait()


def _codec(codec: str) -> str:
    for prefix, normalized in (("avc1", "h264"), ("mp4a", "aac"), ("av01", "av1"), ("vp09", "vp9")):
        if codec.startswith(prefix):
            return normalized
    return codec


def _verify_stage(path: Path, name: str, selected: VideoFormat | AudioTrack, cancel: Event) -> None:
    probe = run_process(
        ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)],
        cancel,
    )
    try:
        info: object = json.loads(probe)
        if not isinstance(info, dict) or not isinstance(info.get("streams"), list):
            raise ValueError
        container = info.get("format")
        if not isinstance(container, dict):
            raise ValueError
        duration = float(container.get("duration", "nan"))
        if not math.isfinite(duration) or duration <= 0 or not path.is_file():
            raise ValueError
        stream = next(
            (
                item
                for item in info["streams"]
                if isinstance(item, dict) and item.get("codec_type") == name
            ),
            None,
        )
        if stream is None or stream.get("codec_name") != _codec(selected.codec):
            raise ValueError
        if isinstance(selected, VideoFormat):
            if stream.get("height") != selected.height:
                raise ValueError
            if selected.fps > 0:
                fps = float(Fraction(str(stream.get("avg_frame_rate", "0"))))
                if abs(fps - selected.fps) > 0.1:
                    raise ValueError
        if path.stat().st_size == 0:
            raise ValueError
    except (ValueError, TypeError, ZeroDivisionError, OSError):
        raise SourceError("invalid_media", "下載產物驗證失敗") from None


class YtDlpAdapter:
    def _api(self, request: dict[str, object], cancel: Event) -> dict[str, object]:
        output = run_process([sys.executable, "-c", _HELPER], cancel, request=json.dumps(request))
        try:
            parsed: object = json.loads(output)
        except ValueError:
            raise SourceError("source_unavailable", "來源不支援／無法取得") from None
        if not isinstance(parsed, dict):
            raise SourceError("source_unavailable", "來源不支援／無法取得")
        return parsed

    def query(self, url: str, cancel: Event | None = None) -> SourceMetadata:
        _, canonical = parse_youtube_url(url)
        return parse_metadata(
            self._api({"mode": "query", "url": canonical}, cancel or Event()), canonical
        )

    def download_subtitle(
        self, source: SourceMetadata, track: SubtitleTrack, cancel: Event
    ) -> bytes:
        from tempfile import TemporaryDirectory

        if track not in source.subtitles or "vtt" not in track.extensions:
            raise SourceError("subtitle_unavailable", "字幕格式無法取得，請手動重試")
        with TemporaryDirectory(prefix="vcc-subtitle-") as directory:
            self._api(
                {
                    "mode": "subtitle",
                    "url": source.source_url,
                    "language": track.language,
                    "automatic": track.automatic,
                    "output": str(Path(directory) / "subtitle.%(ext)s"),
                },
                cancel,
            )
            files = list(Path(directory).glob("*.vtt"))
            if len(files) != 1 or files[0].stat().st_size > 10 * 1024 * 1024:
                raise SourceError("subtitle_unavailable", "字幕無法取得，請手動重試")
            return files[0].read_bytes()

    def download(
        self,
        source: SourceMetadata,
        format_id: str,
        audio_id: str,
        directory: Path,
        cancel: Event,
        progress: Progress,
        stages: DownloadStages | None = None,
    ) -> DownloadedMedia:
        if cancel.is_set():
            raise SourceError("cancelled", "工作已取消")
        refreshed = self.query(source.source_url, cancel)
        video = next((item for item in refreshed.formats if item.id == format_id), None)
        audio = next((item for item in refreshed.audio_tracks if item.id == audio_id), None)
        if video is None or audio is None:
            raise SourceError("format_unavailable", "來源格式已消失，請重新選擇畫質／音軌")
        directory.mkdir(mode=0o700, parents=True, exist_ok=True)
        paths: list[Path] = []
        for name, identifier in (("video", format_id), ("audio", audio_id)):
            progress(f"downloading_{name}", None, None)
            path = stages.load(name, identifier) if stages is not None else None
            if path is None:
                result = self._api(
                    {
                        "mode": "download",
                        "url": source.source_url,
                        "format": identifier,
                        "output": str(directory / f"{name}.%(ext)s"),
                    },
                    cancel,
                )
                path = Path(_string(result.get("path"))).resolve()
                if not path.is_file() or path.parent != directory.resolve():
                    raise SourceError("invalid_media", "下載產物驗證失敗")
                _verify_stage(path, name, video if name == "video" else audio, cancel)
                if stages is not None:
                    if cancel.is_set():
                        raise SourceError("cancelled", "工作已取消")
                    stages.save(name, identifier, path)
            else:
                _verify_stage(path, name, video if name == "video" else audio, cancel)
            paths.append(path)
        browser_playable = _codec(video.codec) == "h264" and _codec(audio.codec) == "aac"
        container = "mp4" if browser_playable else "mkv"
        output = directory / f"source.{container}"
        progress("merging", None, None)
        run_process(
            [
                "ffmpeg",
                "-nostdin",
                "-v",
                "error",
                "-i",
                str(paths[0]),
                "-i",
                str(paths[1]),
                "-map",
                "0:v:0",
                "-map",
                "1:a:0",
                "-c",
                "copy",
                "-n",
                str(output),
            ],
            cancel,
        )
        progress("verifying", None, None)
        probe = run_process(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)],
            cancel,
        )
        try:
            info: object = json.loads(probe)
            if not isinstance(info, dict) or not isinstance(info.get("streams"), list):
                raise ValueError
            codecs = {
                stream.get("codec_type"): stream.get("codec_name")
                for stream in info["streams"]
                if isinstance(stream, dict)
            }
            if codecs.get("video") != _codec(video.codec) or codecs.get("audio") != _codec(
                audio.codec
            ):
                raise ValueError
            raw_container = info.get("format")
            if not isinstance(raw_container, dict):
                raise ValueError
            duration = float(raw_container.get("duration", "nan"))
            if not math.isfinite(duration) or duration <= 0:
                raise ValueError
            streams = info["streams"]
            selected_video = next(
                (
                    stream
                    for stream in streams
                    if isinstance(stream, dict) and stream.get("codec_type") == "video"
                ),
                {},
            )
            if selected_video.get("height") != video.height:
                raise ValueError
            if video.fps > 0:
                actual_fps = float(Fraction(str(selected_video.get("avg_frame_rate", "0"))))
                if abs(actual_fps - video.fps) > 0.1:
                    raise ValueError
            if not output.is_file() or output.stat().st_size == 0:
                raise ValueError
        except (ValueError, TypeError, ZeroDivisionError, OSError):
            raise SourceError("invalid_media", "下載產物驗證失敗") from None
        return DownloadedMedia(
            path=output,
            container=container,
            video_codec=_codec(video.codec),
            audio_codec=_codec(audio.codec),
            browser_playable=browser_playable,
        )
