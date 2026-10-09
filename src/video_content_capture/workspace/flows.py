"""One-click flow: freeze the confirm-screen choices, then chain the existing jobs.

Nothing here reimplements downloading, subtitle acquisition, translation or export; the
coordinator only decides which existing job kind comes next and owns the flow record.
"""

import json
from collections.abc import Callable
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from video_content_capture.workspace.config import WorkspaceSettings
from video_content_capture.workspace.exports import ExportService, ExportSnapshot, SnapshotTrack
from video_content_capture.workspace.jobs import fingerprint
from video_content_capture.workspace.quality import (
    SubtitleForm,
    available_resolutions,
    default_audio,
    default_resolution,
    resolve_source,
)
from video_content_capture.workspace.storage import Library, Record
from video_content_capture.workspace.subtitles import (
    AcquisitionService,
    matching_tracks,
    validate_language,
)
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
FLOW_CANCEL_MESSAGE = "這條流程已經結束"
FLOW_RUNNING_MESSAGE = "這條流程仍在進行"
FLOW_DONE_MESSAGE = "這條流程已經完成"
FLOW_NO_STAGE_MESSAGE = "這條流程沒有可重試的階段工作，請重新開始"
FLOW_MKV_MESSAGE = "請改選 MKV，重新確認摘要並建立新的匯出工作"
FLOW_UNBOUND_MESSAGE = "取消功能尚未啟用"


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
    # Only set when a complete original already existed at start; a version the flow
    # acquires itself is identified through that acquisition job instead.
    source_version_id: str | None = Field(default=None, max_length=32)


def flow_steps(translate: bool) -> list[str]:
    """Planned stages; the translation stage disappears for a same-language target."""
    steps = ["download", "subtitles"]
    if translate:
        steps.append("translation")
    return [*steps, "export"]


def same_language(left: str | None, right: str | None) -> bool:
    """Exact tags after case folding: zh-TW and zh-CN stay different languages."""
    return left is not None and right is not None and left.casefold() == right.casefold()


def bilingual_burning_supported() -> bool:
    """Probe the export contract: bilingual burning arrives with the #5 export change."""
    try:
        ExportSnapshot(
            asset_id="probe",
            source_version_id=None,
            target_version_ids=["probe"],
            target_languages=["zh-TW"],
            include_original=True,
            original_version_id=None,
            container="mp4",
            tracks=[
                SnapshotTrack(version_id="probe", language="zh-TW", name="target"),
                SnapshotTrack(version_id="origin", language="en", name="original"),
            ],
            title="probe",
            youtube_id="abcdefghijk",
            quality="1080p-30fps",
            subtitle_form="burned",
        )
    except ValueError:
        return False
    return True


# Frozen once for the process: the confirm screen and start must agree.
BILINGUAL_BURNED = bilingual_burning_supported()


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
        self.cancel_job: Callable[[str], None] | None = None

    def bind(self, enqueue: Callable[[Record], None]) -> None:
        """The coordinator runs inside the worker's completion hook, so enqueue is injected."""
        self.enqueue = enqueue

    def bind_cancel(self, cancel: Callable[[str], None]) -> None:
        """Cancelling a stage must terminate its child process, which only the queue can do."""
        self.cancel_job = cancel

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

    def _frozen_source_version(self, video: Record) -> str | None:
        """An already selected complete original is part of the frozen plan (人工／匯入)."""
        version = self.library.translation_source_version(str(video["id"]))
        if version is None or not version["complete"] or version["source_type"] == "translation":
            return None
        return str(version["id"])

    def confirm(
        self, video_id: str, height: int | None = None, target_language: str | None = None
    ) -> Record:
        """Read-only confirm screen: what the flow will do before the user presses 開始處理."""
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
            "audio": default_audio(source).model_dump(mode="json"),
            "original_language": source.original_language,
            "original_source": original,
            "original_source_label": ORIGINAL_SOURCE_LABELS.get(original, original),
            # The default form is burned (ADR 0004), so MP4 is the headline output format.
            "subtitle_form": "burned",
            "subtitle_forms": {
                "burned": burned.output_container,
                "tracks": tracks.output_container,
            },
            "output_format": burned.output_container,
            "steps": flow_steps(True if translate is None else translate),
            "needs_translation": translate,
            # 「原文＋目標」 only means something when a translation actually happens.
            "bilingual_allowed": translate is not False,
            "bilingual_burned": BILINGUAL_BURNED,
            "gemini_configured": bool(self.settings.gemini_api_key),
            "blocked_reason": MISSING_KEY_CODE if missing else None,
            "busy": self.library.active_flow(video_id) is not None,
            "latest_flow_id": (
                str(latest["id"]) if (latest := self.library.latest_flow(video_id)) else None
            ),
        }

    def _expected_original(self, video: Record, source: SourceMetadata) -> str:
        selected = self._frozen_source_version(video)
        if selected is not None:
            return str(self.library.get_subtitle_version(selected)["source_type"])
        language = source.original_language
        tracks = matching_tracks(source.subtitles, language)
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
        if choice.subtitle_form == "burned" and choice.include_original and not BILINGUAL_BURNED:
            raise FlowError("燒錄字幕目前僅支援單一語系，雙語請改用可選字幕軌或取消「原文＋目標」")
        resolved = resolve_source(source, choice.height, choice.subtitle_form)
        snapshot = FlowSnapshot(
            **choice.model_dump(),
            format_id=resolved.format_id,
            audio_id=resolved.audio_id,
            container=resolved.container,
            translate=translate,
            source_version_id=self._frozen_source_version(video),
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
        """Completion hook; a broken chain fails the flow rather than the worker."""
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

    def _stage_job(self, flow_id: str, stage: str) -> Record | None:
        """The job that carries the flow's current stage, if it was ever created."""
        for job in self.library.flow_jobs(flow_id):
            if str(job["kind"]) == stage:
                return job
        return None

    def cancel(self, flow_id: str) -> Record:
        """End a running flow and terminate the child process of its current stage.

        `MediaQueue.cancel` signals a running attempt and finishes a queued one itself, and
        either way `advance` turns the cancelled stage into a cancelled flow. That cascade
        runs on the worker thread, so the flow is ended here as well to make the answer
        deterministic.
        """
        flow = self.library.get_flow(flow_id)
        if flow["status"] != "running":
            raise FlowError(FLOW_CANCEL_MESSAGE)
        job = self._stage_job(flow_id, str(flow["stage"]))
        if job is not None and str(job["status"]) in {"queued", "running"}:
            if self.cancel_job is None:
                # Without the queue hook a "cancelled" flow could leave ffmpeg running.
                raise FlowError(FLOW_UNBOUND_MESSAGE)
            try:
                self.cancel_job(str(job["id"]))
            except ValueError:
                # The stage finished between the read above and the cancel, so there is no
                # process left to terminate; fall through and end the flow here instead.
                pass
        # The worker turns a cancelled stage into a cancelled flow, but it does so on its own
        # thread. Ending the flow here as well makes the answer deterministic and keeps a dead
        # worker from leaving a `running` flow that blocks every later start. When the worker
        # gets there first this is a no-op: `update_flow` only writes `running` rows.
        if self.library.get_flow(flow_id)["status"] == "running":
            self.library.update_flow(flow_id, status="cancelled", error="cancelled")
        return self.status(flow_id)

    def retry(self, flow_id: str) -> Record:
        """Resume an ended flow from its failed stage, reusing every finished artifact."""
        flow = self.library.get_flow(flow_id)
        status = str(flow["status"])
        if status == "running":
            raise FlowError(FLOW_RUNNING_MESSAGE)
        if status == "completed":
            raise FlowError(FLOW_DONE_MESSAGE)
        job = self._stage_job(flow_id, str(flow["stage"]))
        if job is None:
            # Reopening without work would leave a running flow blocking every later start.
            raise FlowError(FLOW_NO_STAGE_MESSAGE)
        if str(job["kind"]) == "translation" and not self.settings.gemini_api_key:
            raise FlowKeyError(MISSING_KEY_MESSAGE)
        if str(job["error_code"]) == "container_confirmation_required":
            raise FlowError(FLOW_MKV_MESSAGE)
        # Reopen first: a fast worker must not finish the job before the flow accepts it.
        self.library.reopen_flow(flow_id)
        if str(job["status"]) == "completed":
            # The stage succeeded and chaining failed; redo the chaining, not the stage.
            return self.status(str(self.advance(flow_id, job)["id"]))
        self.library.retry_job(str(job["id"]))
        self.enqueue(self.library.get_job(str(job["id"])))
        return self.status(flow_id)

    def advance(self, flow_id: str, job: Record) -> Record:
        """On success create the next existing job kind; otherwise end the flow here."""
        flow = self.library.get_flow(flow_id)
        if flow["status"] != "running" or str(flow["stage"]) != str(job["kind"]):
            return flow
        status = str(job["status"])
        if status == "cancelled":
            # The user cancelled this stage from the job list; that ends the whole flow.
            return self.library.update_flow(flow_id, status="cancelled", error="cancelled")
        if status != "completed":
            return self.library.update_flow(
                flow_id, status="failed", error=str(job["error_code"] or status)
            )
        video_id = str(flow["video_id"])
        snapshot = FlowSnapshot.model_validate_json(str(flow["snapshot"]))
        kind = str(job["kind"])
        if kind == "export":
            return self.library.update_flow(flow_id, status="completed")
        if kind == "translation":
            translated = json.loads(str(job["snapshot"]))
            return self._chain(
                flow_id,
                "export",
                self._export_job(
                    flow,
                    snapshot,
                    str(translated["source_version_id"]),
                    str(translated["target_version_id"]),
                ),
            )
        if kind == "subtitles":
            return self._after_subtitles(flow, snapshot, job)
        asset_id = self._asset_id(video_id, str(job["format_id"]), str(job["audio_id"]))
        return self._chain(flow_id, "subtitles", self.acquisition.create(video_id, asset_id))

    def _after_subtitles(self, flow: Record, snapshot: FlowSnapshot, job: Record) -> Record:
        flow_id, video_id = str(flow["id"]), str(flow["video_id"])
        source_version_id = self._acquired_source(flow_id, snapshot, job)
        language = str(self.library.get_subtitle_version(source_version_id)["language"])
        if snapshot.translate and not same_language(language, snapshot.target_language):
            translation = self.translation.create(
                video_id, source_version_id, snapshot.target_language
            )
            if translation["status"] == "completed":
                # An identical finished translation was reused; skip straight to export.
                target_version_id = str(
                    json.loads(str(translation["snapshot"]))["target_version_id"]
                )
                return self._chain(
                    flow_id,
                    "export",
                    self._export_job(flow, snapshot, source_version_id, target_version_id),
                )
            return self._chain_translation(flow_id, translation)
        # The acquired original already is the target language: nothing to translate.
        return self._chain(
            flow_id,
            "export",
            self._export_job(flow, snapshot, source_version_id, source_version_id),
        )

    def _chain_translation(self, flow_id: str, translation: Record) -> Record:
        self.library.set_job_flow(str(translation["id"]), flow_id)
        self.library.update_flow(flow_id, stage="translation")
        self.enqueue(translation)
        return self.library.get_flow(flow_id)

    def _acquired_source(self, flow_id: str, snapshot: FlowSnapshot, job: Record) -> str:
        """Which original the flow translates: frozen at start, else its own acquisition job."""
        if snapshot.source_version_id is not None:
            return snapshot.source_version_id
        # Match the version this acquisition job published, not the UI's current selection,
        # so a reselection made while the flow runs cannot change what it translates.
        acquired = json.loads(str(job["snapshot"]))
        wanted = acquired.get("language")
        acquired_kind = acquired.get("source_type")
        candidates = [
            version
            for version in self.library.subtitle_versions(str(job["video_id"]))
            if version["complete"]
            and version["source_type"] != "translation"
            and (wanted is None or version["language"] == wanted)
            and (acquired_kind is None or version["source_type"] == acquired_kind)
        ]
        if candidates:
            return str(candidates[-1]["id"])
        raise FlowError("找不到這條流程取得的原文字幕版本")

    def _chain(self, flow_id: str, stage: str, job: Record) -> Record:
        self.library.set_job_flow(str(job["id"]), flow_id)
        self.library.update_flow(flow_id, stage=stage)
        self.enqueue(job)
        return self.library.get_flow(flow_id)

    def _export_job(
        self, flow: Record, snapshot: FlowSnapshot, source_version_id: str, target_version_id: str
    ) -> Record:
        video_id, flow_id = str(flow["video_id"]), str(flow["id"])
        asset_id = self._asset_for_flow(flow_id, video_id)
        preview = self.exporter.preview(
            video_id,
            asset_id,
            [target_version_id],
            snapshot.include_original,
            source_version_id if snapshot.include_original else None,
            source_version_id,
            None,
            snapshot.subtitle_form,
        )
        return self.exporter.create(video_id, preview)

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

    def status(self, flow_id: str) -> Record:
        """Per-stage progress plus the finished artifact the UI offers as 「下載影片」."""
        flow = self.library.get_flow(flow_id)
        snapshot = FlowSnapshot.model_validate_json(str(flow["snapshot"]))
        jobs = {str(job["kind"]): job for job in self.library.flow_jobs(flow_id)}
        stages = [
            {
                "stage": stage,
                "status": str(jobs[stage]["status"]) if stage in jobs else "pending",
                "detail": str(jobs[stage]["stage"]) if stage in jobs else None,
                "progress": jobs[stage]["progress"] if stage in jobs else None,
                "error_code": jobs[stage]["error_code"] if stage in jobs else None,
                "job_id": str(jobs[stage]["id"]) if stage in jobs else None,
            }
            for stage in flow_steps(snapshot.translate)
        ]
        artifact = None
        export_job = jobs.get("export")
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
    "BILINGUAL_BURNED",
    "BUSY_MESSAGE",
    "FlowChoice",
    "FlowConflictError",
    "FlowError",
    "FlowKeyError",
    "FlowService",
    "FlowSnapshot",
    "MISSING_KEY_CODE",
    "MISSING_KEY_MESSAGE",
    "bilingual_burning_supported",
    "flow_steps",
    "same_language",
]
