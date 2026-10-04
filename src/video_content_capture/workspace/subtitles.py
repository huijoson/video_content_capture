"""Bounded plain-text subtitle parsing and multilingual source acquisition."""

import math
import re
from collections.abc import Callable
from html import escape
from html.parser import HTMLParser
from pathlib import Path
from threading import Event
from typing import TYPE_CHECKING, Protocol

from pydantic import BaseModel, ConfigDict

from video_content_capture.redaction import scrub_text
from video_content_capture.workspace.youtube import (
    SourceError,
    SourceMetadata,
    SubtitleTrack,
    YoutubeAdapter,
)

if TYPE_CHECKING:
    from video_content_capture.workspace.storage import Library

MAX_SUBTITLE_BYTES = 10 * 1024 * 1024
MAX_CUES = 100_000
_LANGUAGE = re.compile(r"[A-Za-z]{2,8}(?:-[A-Za-z0-9]{1,8})*\Z")
_TIMESTAMP = re.compile(r"(?:(\d+):)?(\d{2}):(\d{2})[,.](\d{3})\Z")


class Cue(BaseModel):
    model_config = ConfigDict(frozen=True)
    id: str
    start: float
    end: float
    text: str


class ParsedSubtitles(BaseModel):
    cues: list[Cue]
    warnings: list[str]


class _PlainText(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.text: list[str] = []
        self.blocked = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in {"script", "style"}:
            self.blocked += 1
        elif tag == "br" and not self.blocked:
            self.text.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in {"script", "style"} and self.blocked:
            self.blocked -= 1

    def handle_data(self, data: str) -> None:
        if not self.blocked:
            self.text.append(data)


def plain_text(text: str) -> str:
    parser = _PlainText()
    parser.feed(text)
    return scrub_text("".join(parser.text)).strip()


def validate_language(language: str) -> str:
    if language != "und" and _LANGUAGE.fullmatch(language) is None:
        raise ValueError("Invalid subtitle language")
    return language


def validate_cues(cues: list[Cue], duration: float) -> ParsedSubtitles:
    if not cues or len(cues) > MAX_CUES:
        raise ValueError("Subtitle must contain 1–100,000 cues")
    result: list[Cue] = []
    warnings: list[str] = []
    ids: set[str] = set()
    for index, cue in enumerate(cues, 1):
        if cue.id in ids or not cue.id or "\n" in cue.id or "-->" in cue.id:
            raise ValueError(f"cue {index}: invalid or duplicate cue ID")
        ids.add(cue.id)
        if not (math.isfinite(cue.start) and math.isfinite(cue.end)):
            raise ValueError(f"cue {index}: nonfinite time")
        if cue.start < 0 or cue.start >= cue.end or cue.start >= duration:
            raise ValueError(f"cue {index}: invalid time")
        if cue.end > duration + 1:
            raise ValueError(f"cue {index}: exceeds video duration")
        if cue.end > duration:
            warnings.append(f"cue {index}: end clipped to video duration")
        text = plain_text(cue.text)
        if not text:
            raise ValueError(f"cue {index}: empty text")
        result.append(cue.model_copy(update={"end": min(cue.end, duration), "text": text}))
    return ParsedSubtitles(cues=sorted(result, key=lambda cue: cue.start), warnings=warnings)


def _time(value: str) -> float:
    match = _TIMESTAMP.fullmatch(value)
    if match is None:
        raise ValueError("invalid timestamp")
    hours, minutes, seconds, milliseconds = match.groups()
    if int(minutes) >= 60 or int(seconds) >= 60:
        raise ValueError("invalid timestamp")
    return int(hours or 0) * 3600 + int(minutes) * 60 + int(seconds) + int(milliseconds) / 1000


def parse_subtitles(data: bytes, format: str, duration: float) -> ParsedSubtitles:
    if len(data) > MAX_SUBTITLE_BYTES:
        raise ValueError("Subtitle exceeds 10 MiB")
    if not math.isfinite(duration) or duration <= 0:
        raise ValueError("Invalid video duration")
    if format.lower() not in {"srt", "vtt"}:
        raise ValueError("Only SRT and WebVTT are supported")
    try:
        text = data.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n")
    except UnicodeDecodeError:
        raise ValueError("Subtitle must be UTF-8") from None
    lines = text.splitlines()
    vtt = format.lower() == "vtt"
    if vtt and (not lines or not re.fullmatch(r"WEBVTT(?:[ \t].*)?", lines[0])):
        raise ValueError("cue 1 line 1: missing WEBVTT header")
    index = 1 if vtt else 0
    cues: list[Cue] = []
    warnings: list[str] = []
    while index < len(lines):
        if not lines[index].strip():
            index += 1
            continue
        block_line = index + 1
        block: list[str] = []
        while index < len(lines) and lines[index].strip():
            block.append(lines[index])
            index += 1
        if vtt and (block[0].startswith("NOTE") or block[0] in {"STYLE", "REGION"}):
            continue
        number = len(cues) + 1
        timing_index = 0 if "-->" in block[0] else 1
        line_number = block_line + timing_index
        try:
            if not vtt and timing_index == 1 and not block[0].isdigit():
                raise ValueError("invalid SRT cue number")
            if timing_index >= len(block):
                raise ValueError("missing timestamp")
            timing = re.fullmatch(r"(\S+)\s+-->\s+(\S+)(?:\s+.*)?", block[timing_index])
            if timing is None:
                raise ValueError("invalid timestamp")
            cue = Cue(
                id=f"c{number:06d}",
                start=_time(timing[1]),
                end=_time(timing[2]),
                text="\n".join(block[timing_index + 1 :]),
            )
            checked = validate_cues([cue], duration)
            cues.extend(checked.cues)
            warnings.extend(checked.warnings)
            if len(cues) > MAX_CUES:
                raise ValueError("exceeds 100,000 cues")
        except ValueError as exc:
            raise ValueError(f"cue {number} line {line_number}: {exc}") from None
    if not cues:
        raise ValueError("cue 1 line 1: no subtitle cues")
    return ParsedSubtitles(cues=sorted(cues, key=lambda cue: cue.start), warnings=warnings)


def _timestamp(seconds: float, separator: str) -> str:
    milliseconds = round(seconds * 1000)
    hours, remainder = divmod(milliseconds, 3600000)
    minutes, remainder = divmod(remainder, 60000)
    seconds_int, milliseconds = divmod(remainder, 1000)
    return f"{hours:02}:{minutes:02}:{seconds_int:02}{separator}{milliseconds:03}"


def render_subtitles(cues: list[Cue], format: str = "vtt") -> bytes:
    if format not in {"vtt", "srt"}:
        raise ValueError("Only SRT and WebVTT are supported")
    blocks = ["WEBVTT\n"] if format == "vtt" else []
    separator = "." if format == "vtt" else ","
    for index, cue in enumerate(cues, 1):
        content = escape(cue.text, quote=False) if format == "vtt" else cue.text
        blocks.append(
            f"{index}\n{_timestamp(cue.start, separator)} --> "
            f"{_timestamp(cue.end, separator)}\n{content}\n"
        )
    return "\n".join(blocks).encode("utf-8")


class ASRAdapter(Protocol):
    def transcribe(
        self, path: Path, language: str | None, duration: float, cancel: Event
    ) -> tuple[str, list[Cue]]: ...


class MLXSubtitleAdapter:
    """Reuse the local MLX loader without the CLI's Chinese text normalization."""

    def __init__(
        self,
        transcribe_fn: Callable[..., object] | None = None,
        model: str = "mlx-community/whisper-large-v3-turbo",
    ) -> None:
        self.transcribe_fn = transcribe_fn
        self.model = model

    def transcribe(
        self, path: Path, language: str | None, duration: float, cancel: Event
    ) -> tuple[str, list[Cue]]:
        from video_content_capture.transcription.mlx import _load_mlx_transcribe

        if cancel.is_set():
            raise ValueError("Cancelled")
        transcribe = self.transcribe_fn or _load_mlx_transcribe()
        payload = transcribe(
            str(path),
            path_or_hf_repo=self.model,
            language=language.split("-")[0] if language else None,
            temperature=0.0,
            word_timestamps=False,
        )
        if cancel.is_set():
            raise ValueError("Cancelled")
        if not isinstance(payload, dict) or not isinstance(payload.get("segments"), list):
            raise ValueError("Invalid local transcript")
        cues: list[Cue] = []
        for index, segment in enumerate(payload["segments"]):
            if not isinstance(segment, dict) or not isinstance(segment.get("text"), str):
                raise ValueError("Invalid local transcript")
            if not segment["text"].strip():
                continue
            cues.append(
                Cue(
                    id=f"c{index + 1:06d}",
                    start=float(segment["start"]),
                    end=float(segment["end"]),
                    text=segment["text"],
                )
            )
        detected = payload.get("language")
        result_language = detected if isinstance(detected, str) else language or "und"
        return validate_language(result_language), validate_cues(cues, duration).cues


def acquire_subtitles(
    adapter: YoutubeAdapter,
    asr: ASRAdapter,
    source: SourceMetadata,
    media_path: Path,
    cancel: Event,
    track: SubtitleTrack | None = None,
) -> tuple[str, str, ParsedSubtitles]:
    # A fresh query must succeed before absence can trigger local recognition.
    refreshed = adapter.query(source.source_url, cancel)
    language = refreshed.original_language
    selected = track
    if selected is not None:
        requested = selected
        selected = next(
            (
                item
                for item in refreshed.subtitles
                if item.language == requested.language and item.automatic == requested.automatic
            ),
            None,
        )
        if selected is None:
            raise SourceError("subtitle_unavailable", "Platform subtitle unavailable; choose again")
    if selected is None:
        if language is None and refreshed.subtitles:
            raise SourceError(
                "source_language_unknown",
                "Original language unknown; choose a platform subtitle track",
            )
        candidates = [item for item in refreshed.subtitles if item.language == language]
        selected = next((item for item in candidates if not item.automatic), None)
        selected = selected or next(iter(candidates), None)
    if selected is not None:
        data = adapter.download_subtitle(refreshed, selected, cancel)
        return (
            selected.language,
            ("platform_auto" if selected.automatic else "platform_manual"),
            parse_subtitles(data, "vtt", source.duration),
        )
    result_language, cues = asr.transcribe(media_path, language, source.duration, cancel)
    return result_language, "asr", validate_cues(cues, source.duration)


class AcquisitionService:
    def __init__(
        self,
        library: "Library",
        adapter: YoutubeAdapter,
        asr: ASRAdapter | None = None,
        scrub: Callable[[str], str] = scrub_text,
    ) -> None:
        self.library = library
        self.adapter = adapter
        self.asr = asr or MLXSubtitleAdapter()
        self.scrub = scrub

    def create(
        self,
        video_id: str,
        asset_id: str,
        language: str | None = None,
        source_type: str | None = None,
    ) -> dict[str, object]:
        asset = self.library.get_asset(asset_id)
        if asset["video_id"] != video_id:
            raise ValueError("Asset belongs to another video")
        if language is not None:
            validate_language(language)
        if source_type not in {None, "platform_manual", "platform_auto"}:
            raise ValueError("Invalid platform subtitle source")
        return self.library.create_snapshot_job(
            video_id,
            "subtitles",
            {"asset_id": asset_id, "language": language, "source_type": source_type},
        )

    def run(self, job: dict[str, object], attempt: str, cancel: Event) -> None:
        import json
        import sqlite3

        from video_content_capture.workspace.jobs import checksum

        if cancel.is_set():
            return
        snapshot = json.loads(str(job["snapshot"]))
        video_id = str(job["video_id"])
        video = self.library.get_video(video_id)
        # Explicit imported sources are authoritative and never fall back to the platform.
        selected_id = video["translation_source_version_id"]
        if selected_id is not None and snapshot["language"] is None:
            self.library.publish_attempt(
                str(job["id"]),
                attempt,
                lambda connection: None,
                owner_validator=lambda connection: not cancel.is_set(),
            )
            return
        source = SourceMetadata.model_validate_json(str(video["metadata"]))
        asset = self.library.get_asset(snapshot["asset_id"])
        try:
            if asset["video_id"] != video_id:
                raise ValueError("Asset belongs to another video")
            relative = Path(str(asset["path"]))
            with self.library._directory(relative.parent):
                media_path = self.library.root / relative
                if (
                    media_path.is_symlink()
                    or not media_path.is_file()
                    or checksum(media_path) != asset["checksum"]
                ):
                    raise ValueError("Invalid source asset; reacquire media")
            track = None
            if snapshot["language"] is not None:
                track = next(
                    (
                        item
                        for item in source.subtitles
                        if item.language == snapshot["language"]
                        and item.automatic == (snapshot["source_type"] == "platform_auto")
                    ),
                    None,
                )
                if track is None:
                    raise ValueError("Platform subtitle track unavailable")
            language, source_type, parsed = acquire_subtitles(
                self.adapter,
                self.asr,
                source,
                media_path,
                cancel,
                track,
            )
            if cancel.is_set():
                return
            safe_language = self.scrub(language)
            validate_language(safe_language)
            cues = [cue.model_copy(update={"text": self.scrub(cue.text)}) for cue in parsed.cues]

            def publish(connection: sqlite3.Connection) -> None:
                version = self.library.create_subtitle_version(
                    video_id,
                    safe_language,
                    f"{safe_language} {source_type}",
                    source_type,
                    cues,
                    connection=connection,
                )
                connection.execute(
                    "UPDATE videos SET translation_source_version_id=?, "
                    "qa_version_id=? WHERE id=? AND translation_source_version_id IS NULL",
                    (version["id"], version["id"], video_id),
                )

            self.library.publish_attempt(
                str(job["id"]),
                attempt,
                publish,
                owner_validator=lambda connection: not cancel.is_set(),
            )
        except SourceError as error:
            self.library.update_attempt(str(job["id"]), attempt, "subtitles", error=error.code)
        except Exception:
            self.library.update_attempt(
                str(job["id"]), attempt, "subtitles", error="subtitle_acquisition_failed"
            )
