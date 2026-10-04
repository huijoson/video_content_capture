"""One-click flow: freeze the confirm-screen choices, then chain the existing jobs.

Nothing here reimplements downloading, subtitle acquisition, translation or export; the
coordinator only decides which existing job kind comes next and owns the flow record.
"""

import json
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from video_content_capture.workspace.config import WorkspaceSettings
from video_content_capture.workspace.exports import ExportService
from video_content_capture.workspace.jobs import fingerprint
from video_content_capture.workspace.quality import (
    SubtitleForm,
    available_resolutions,
    default_audio,
    default_resolution,
    resolve_source,
)
from video_content_capture.workspace.storage import Library, Record
from video_content_capture.workspace.subtitles import AcquisitionService, validate_language
from video_content_capture.workspace.translation import TranslationService
from video_content_capture.workspace.youtube import SourceMetadata

# The confirm screen names the expected original-subtitle source in plain Chinese.
ORIGINAL_SOURCE_LABELS: Record = {
    "platform_manual": "人工",
    "platform_auto": "自動",
    "asr": "本機辨識",
    "import": "匯入",
}
MISSING_KEY_CODE = "missing_gemini_key"
MISSING_KEY_MESSAGE = "請在專案 .env 設定 GEMINI_API_KEY 並重啟服務"
BUSY_MESSAGE = "此影片的一鍵流程正在進行中，請等待完成或先取消目前的工作"
RESOLUTION_MESSAGE = "請選擇來源實際有的解析度"
TARGET_MESSAGE = "請選擇有效的目標語系"


class FlowError(ValueError):
    """A confirm-screen choice was rejected before anything started."""


class FlowKeyError(FlowError):
    """Translation is required but no usable Gemini key is configured."""


class FlowConflictError(FlowError):
    """Another flow is already running for this video."""


class FlowChoice(BaseModel):
    """The confirm-screen choices, frozen at start time so later UI edits cannot matter."""

    model_config = ConfigDict(frozen=True, extra="forbid")
    height: int = Field(ge=1, le=100_000)
    target_language: str = Field(min_length=1, max_length=50)
    include_original: bool = False
    # ADR 0004: the one-click flow burns subtitles by default (#2 kept "tracks" for downloads).
    subtitle_form: SubtitleForm = "burned"


class FlowSnapshot(FlowChoice):
    """Frozen choices plus the resolved source and the stage plan the coordinator follows."""

    format_id: str = Field(min_length=1, max_length=200)
    audio_id: str = Field(min_length=1, max_length=200)
    container: Literal["mp4", "mkv"]
    translate: bool


def flow_steps(translate: bool) -> list[str]:
    """Planned stages; the translation stage disappears for a same-language target."""
    steps = ["download", "subtitles"]
    if translate:
        steps.append("translation")
    return [*steps, "export"]


def same_language(left: str | None, right: str | None) -> bool:
    """Exact tags after case folding: zh-TW and zh-CN stay different languages."""
    return left is not None and right is not None and left.casefold() == right.casefold()


class FlowService:
    def __init__(
        self,
        library: Library,
        settings: WorkspaceSettings,
        acquisition: AcquisitionService,
        translation: TranslationService,
        exporter: ExportService,
    ) -> None:
        self.library, self.settings = library, settings
        self.acquisition, self.translation, self.exporter = acquisition, translation, exporter
        self.enqueue: Callable[[Record], None] = lambda job: None

    def bind(self, enqueue: Callable[[Record], None]) -> None:
        """The coordinator runs inside the worker's completion hook, so enqueue is injected."""
        self.enqueue = enqueue

    def _video(self, video_id: str) -> Record:
        return self.library.get_video(video_id)

    def _source(self, video: Record) -> SourceMetadata:
        try:
            return SourceMetadata.model_validate_json(str(video["metadata"]))
        except ValueError as error:
            raise FlowError("請先查詢影片來源與字幕，再開始一鍵流程") from error

    def _target_language(self, value: str) -> str:
        try:
            validate_language(value)
        except ValueError as error:
            raise FlowError(TARGET_MESSAGE) from error
        if value == "und":
            raise FlowError(TARGET_MESSAGE)
        return value

    def _resolution(self, source: SourceMetadata, height: int) -> None:
        if height not in available_resolutions(source):
            raise FlowError(RESOLUTION_MESSAGE)

    def _needs_translation(self, source: SourceMetadata, target: str | None) -> bool:
        # An unknown original language cannot be assumed to match the target.
        return target is None or not same_language(source.original_language, target)

    def confirm(
        self, video_id: str, height: int | None = None, target_language: str | None = None
    ) -> Record:
        """Read-only confirm screen: everything the user sees before pressing 開始."""
        video = self._video(video_id)
        source = self._source(video)
        chosen = default_resolution(source) if height is None else height
        self._resolution(source, chosen)
        target = None if target_language is None else self._target_language(target_language)
        original = self._expected_original(video, source)
        translate = None if target is None else self._needs_translation(source, target)
        missing = bool(translate) and not self.settings.gemini_api_key
        burned = resolve_source(source, chosen, "burned")
        tracks = resolve_source(source, chosen, "tracks")
        return {
            "video_id": video_id,
            "title": str(video["title"]),
            "duration": video["duration"],
            "resolution": chosen,
            "default_resolution": default_resolution(source),
            "resolutions": available_resolutions(source),
            "original_language": source.original_language,
            "original_source": original,
            "original_source_label": ORIGINAL_SOURCE_LABELS.get(original, original),
            "audio": default_audio(source).model_dump(mode="json"),
            # The default form is burned (ADR 0004), so MP4 is the headline output format.
            "subtitle_form": "burned",
            "output_format": burned.output_container,
            "output_formats": {"burned": burned.output_container, "tracks": tracks.container},
            "steps": flow_steps(True if translate is None else translate),
            "needs_translation": translate,
            "bilingual_allowed": translate is not False,
            "gemini_configured": bool(self.settings.gemini_api_key),
            "blocked_reason": MISSING_KEY_CODE if missing else None,
            "busy": self.library.active_flow(video_id) is not None,
        }

    def _expected_original(self, video: Record, source: SourceMetadata) -> str:
        selected = video["translation_source_version_id"]
        if selected is not None:
            return str(self.library.get_subtitle_version(str(selected))["source_type"])
        language = source.original_language
        tracks = [track for track in source.subtitles if track.language == language]
        if language is not None and any(not track.automatic for track in tracks):
            return "platform_manual"
        if tracks:
            return "platform_auto"
        return "asr"

    def start(self, video_id: str, choice: FlowChoice) -> tuple[Record, Record]:
        """Freeze the choices, then create the download job the rest of the flow chains from."""
        video = self._video(video_id)
        source = self._source(video)
        target = self._target_language(choice.target_language)
        self._resolution(source, choice.height)
        translate = self._needs_translation(source, target)
        if choice.include_original and not translate:
            raise FlowError("目標語系與原文相同，雙語字幕無法啟用，請改用單一語系")
        if translate and not self.settings.gemini_api_key:
            raise FlowKeyError(MISSING_KEY_MESSAGE)
        if choice.subtitle_form == "burned" and choice.include_original:
            raise FlowError("燒錄字幕目前僅支援單一語系，雙語請改用可選字幕軌或取消「原文＋目標」")
        resolved = resolve_source(source, choice.height, choice.subtitle_form)
        snapshot = FlowSnapshot(
            **choice.model_dump(),
            format_id=resolved.format_id,
            audio_id=resolved.audio_id,
            container=resolved.container,
            translate=translate,
        )
        if self.library.active_flow(video_id) is not None:
            raise FlowConflictError(BUSY_MESSAGE)
        try:
            flow = self.library.create_flow(video_id, "download", snapshot.model_dump())
        except ValueError as error:
            raise FlowConflictError(BUSY_MESSAGE) from error
        job = self.library.create_job(
            video_id, resolved.format_id, resolved.audio_id, str(flow["id"])
        )
        self.enqueue(job)
        return flow, job

    def on_job_finished(self, job_id: str) -> None:
        """Completion hook: a broken chain fails the flow instead of the worker."""
        try:
            job = self.library.get_job(job_id)
        except ValueError:
            return
        if job["flow_id"] is None:
            return
        flow_id = str(job["flow_id"])
        try:
            self.advance(flow_id, job)
        except Exception:
            self.library.update_flow(flow_id, status="failed", error="flow_failed")

    def advance(self, flow_id: str, job: Record) -> Record:
        """On success create the next existing job kind; otherwise end the flow here."""
        flow = self.library.get_flow(flow_id)
        if flow["status"] != "running" or str(flow["stage"]) != str(job["kind"]):
            return flow
        status = str(job["status"])
        if status != "completed":
            return self.library.update_flow(
                flow_id, status="failed", error=str(job["error_code"] or status)
            )
        video_id = str(flow["video_id"])
        snapshot = FlowSnapshot.model_validate_json(str(flow["snapshot"]))
        if job["kind"] == "export":
            return self.library.update_flow(flow_id, status="completed")
        if job["kind"] == "subtitles":
            source_version_id = self._source_version_id(video_id)
            language = self._language(source_version_id)
            if snapshot.translate and not same_language(language, snapshot.target_language):
                job = self.translation.create(video_id, source_version_id, snapshot.target_language)
                return self._chain(flow_id, "translation", job)
            # The acquired original already is the target language: nothing to translate.
            return self._chain(flow_id, "export", self._export_job(flow, snapshot))
        asset_id = self._asset_id(video_id, str(job["format_id"]), str(job["audio_id"]))
        return self._chain(flow_id, "subtitles", self.acquisition.create(video_id, asset_id))

    def _chain(self, flow_id: str, stage: str, job: Record) -> Record:
        self.library.set_job_flow(str(job["id"]), flow_id)
        self.library.update_flow(flow_id, stage=stage)
        self.enqueue(job)
        return self.library.get_flow(flow_id)

    def _export_job(self, flow: Record, snapshot: FlowSnapshot) -> Record:
        video_id = str(flow["video_id"])
        flow_id = str(flow["id"])
        source_version_id = self._source_version_id(video_id)
        asset_id = self._asset_for_flow(flow_id, video_id)
        # Only a translated export is bilingual; the original track is the frozen source version.
        preview = self.exporter.preview(
            video_id,
            asset_id,
            [self._target_version_id(flow_id, source_version_id)],
            snapshot.include_original,
            source_version_id if snapshot.include_original else None,
            source_version_id,
            None,
            snapshot.subtitle_form,
        )
        return self.exporter.create(video_id, preview)

    def _target_version_id(self, flow_id: str, source_version_id: str) -> str:
        for job in self.library.flow_jobs(flow_id):
            if job["kind"] == "translation" and job["status"] == "completed":
                return str(json.loads(str(job["snapshot"]))["target_version_id"])
        return source_version_id

    def _asset_for_flow(self, flow_id: str, video_id: str) -> str:
        for job in self.library.flow_jobs(flow_id):
            if job["kind"] == "download" and job["status"] == "completed":
                return self._asset_id(video_id, str(job["format_id"]), str(job["audio_id"]))
        raise FlowError("找不到這條流程下載的影音")

    def _asset_id(self, video_id: str, format_id: str, audio_id: str) -> str:
        identity = fingerprint(format_id, audio_id)
        for asset in self.library.assets(video_id):
            if asset["fingerprint"] == identity:
                return str(asset["id"])
        raise FlowError("找不到這條流程下載的影音")

    def _source_version_id(self, video_id: str) -> str:
        video = self._video(video_id)
        selected = video["translation_source_version_id"]
        if selected is not None:
            version = self.library.get_subtitle_version(str(selected))
            if version["complete"]:
                return str(selected)
        candidates = [
            version
            for version in self.library.subtitle_versions(video_id)
            if version["complete"] and version["source_type"] != "translation"
        ]
        if not candidates:
            raise FlowError("找不到可翻譯的完整原文字幕版本")
        return str(candidates[-1]["id"])

    def _language(self, version_id: str) -> str:
        return str(self.library.get_subtitle_version(version_id)["language"])

    def status(self, flow_id: str) -> Record:
        """Per-stage progress plus the finished artifact the UI offers as 「下載影片」."""
        flow = self.library.get_flow(flow_id)
        snapshot = FlowSnapshot.model_validate_json(str(flow["snapshot"]))
        jobs = {str(job["kind"]): job for job in self.library.flow_jobs(flow_id)}
        stages: list[Record] = []
        reached = False
        for stage in reversed(flow_steps(snapshot.translate)):
            job = jobs.get(stage)
            reached = reached or job is not None
            stages.append(
                {
                    "stage": stage,
                    "status": str(job["status"]) if job is not None else "pending",
                    "detail": str(job["stage"]) if job is not None else None,
                    "progress": job["progress"] if job is not None else None,
                    "error_code": job["error_code"] if job is not None else None,
                    "job_id": str(job["id"]) if job is not None else None,
                    # An empty stage behind a started later stage was skipped, not pending.
                    "skipped": job is None and stage == "translation" and reached,
                }
            )
        stages.reverse()
        export_job = jobs.get("export")
        artifact = None
        if export_job is not None:
            artifact = next(
                (
                    {key: value for key, value in row.items() if key != "path"}
                    for row in self.library.exports(str(flow["video_id"]))
                    if row["job_id"] == export_job["id"]
                ),
                None,
            )
        return {
            "flow": {key: value for key, value in flow.items() if key != "snapshot"},
            "snapshot": snapshot.model_dump(),
            "stages": stages,
            "artifact": artifact,
        }


__all__ = [
    "BUSY_MESSAGE",
    "FlowChoice",
    "FlowConflictError",
    "FlowError",
    "FlowKeyError",
    "FlowService",
    "FlowSnapshot",
    "MISSING_KEY_CODE",
    "MISSING_KEY_MESSAGE",
    "flow_steps",
    "same_language",
]
