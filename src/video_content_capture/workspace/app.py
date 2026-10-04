"""Local workspace API with independently selected subtitle versions."""

import json
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Annotated, Literal, cast

from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from video_content_capture import __version__
from video_content_capture.workspace.config import WorkspaceSettings, validate_binding
from video_content_capture.workspace.exports import ExportService, MediaExporter
from video_content_capture.workspace.jobs import JobLanes, MediaQueue
from video_content_capture.workspace.previews import Encoder, PreviewService
from video_content_capture.workspace.qa import QAAdapter
from video_content_capture.workspace.qa_jobs import QAService
from video_content_capture.workspace.quality import SubtitleForm, quality_options, resolve_source
from video_content_capture.workspace.security import LocalBoundary, configure_redaction, public_text
from video_content_capture.workspace.storage import Library, Record
from video_content_capture.workspace.subtitles import (
    AcquisitionService,
    ASRAdapter,
    Cue,
    parse_subtitles,
    render_subtitles,
)
from video_content_capture.workspace.translation import TranslationAdapter, TranslationService
from video_content_capture.workspace.youtube import (
    SourceError,
    SourceMetadata,
    YoutubeAdapter,
    YtDlpAdapter,
    parse_youtube_url,
)

# Bounded wait for media workers to stop before managed files are removed.
DELETE_WAIT_SECONDS = 10.0


class GeminiStatus(BaseModel):
    configured: bool
    translation_model: str
    qa_model: str


class ServiceStatus(BaseModel):
    version: str
    gemini: GeminiStatus


class QueryRequest(BaseModel):
    url: str = Field(max_length=2048)
    refresh: bool = False


class JobRequest(BaseModel):
    # The user picks only a resolution; the source format and audio track are resolved.
    height: int | None = Field(default=None, gt=0, le=100_000)
    # Matches the existing export output (selectable tracks) until burned export exists.
    subtitle_form: SubtitleForm = "tracks"
    # Explicit format/audio IDs remain accepted for internal callers and older clients.
    format_id: str | None = Field(default=None, max_length=100)
    audio_id: str | None = Field(default=None, max_length=100)


class PositionRequest(BaseModel):
    position: float = Field(ge=0, allow_inf_nan=False)


class SelectionRequest(BaseModel):
    version_id: str | None = Field(default=None, max_length=32)


class TranslationRequest(BaseModel):
    language: str = Field(max_length=50)
    regenerate: bool = False


class QuestionRequest(BaseModel):
    question: str = Field(min_length=1, max_length=20_000)


class AcquisitionRequest(BaseModel):
    asset_id: str = Field(max_length=32)
    language: str | None = Field(default=None, max_length=50)
    source_type: str | None = Field(default=None, max_length=30)


class ExportRequest(BaseModel):
    asset_id: str = Field(max_length=32)
    target_version_ids: list[str] = Field(min_length=1, max_length=1)
    include_original: bool = False
    original_version_id: str | None = None
    container: str | None = Field(default=None, pattern="^(mp4|mkv)$")
    subtitle_form: Literal["burned", "tracks"] = "tracks"


class ConfirmExportRequest(BaseModel):
    snapshot: dict[str, object]


class PreviewRequest(BaseModel):
    asset_id: str = Field(max_length=32)


class DeleteRequest(BaseModel):
    confirmation: str = Field(min_length=64, max_length=64)


def create_app(
    settings: WorkspaceSettings,
    adapter: YoutubeAdapter | None = None,
    *,
    translation_adapter: TranslationAdapter | None = None,
    qa_adapter: QAAdapter | None = None,
    asr_adapter: ASRAdapter | None = None,
    media_exporter: MediaExporter | None = None,
    preview_encoder: Encoder | None = None,
) -> FastAPI:
    validate_binding(settings.host, settings.port)
    configure_redaction(settings)
    library = Library(settings.library_dir)

    downloader = adapter or YtDlpAdapter()
    translation = TranslationService(library, settings, translation_adapter)
    qa = QAService(library, settings, qa_adapter)
    acquisition = AcquisitionService(
        library, downloader, asr_adapter, scrub=lambda value: public_text(value, settings)
    )
    exporter = ExportService(library, media_exporter)
    previews = PreviewService(library, preview_encoder)
    queue = JobLanes(
        library,
        MediaQueue(
            library,
            downloader,
            handlers={
                "translation": translation.run,
                "subtitles": acquisition.run,
                "export": exporter.run,
                "preview": previews.run,
            },
        ),
        MediaQueue(library, downloader, handlers={"qa": qa.run}, name="workspace-qa"),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncIterator[None]:
        library.initialize()
        queue.start()
        try:
            yield
        finally:
            queue.close()

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.add_middleware(LocalBoundary, settings=settings)
    static = Path(__file__).parent / "static"

    @app.get("/api/status")
    def status() -> ServiceStatus:
        return ServiceStatus(
            version=__version__,
            gemini=GeminiStatus(
                configured=settings.gemini_api_key is not None,
                translation_model=public_text(settings.translation_model, settings),
                qa_model=public_text(settings.qa_model, settings),
            ),
        )

    @app.get("/api/videos")
    def videos() -> list[Record]:
        return library.list_videos()

    def safe_source(source: SourceMetadata) -> SourceMetadata:
        def scrub(value: object) -> object:
            if isinstance(value, str):
                return public_text(value, settings)
            if isinstance(value, dict):
                return {key: scrub(item) for key, item in value.items()}
            if isinstance(value, list):
                return [scrub(item) for item in value]
            return value

        sanitized = SourceMetadata.model_validate(scrub(source.model_dump(mode="json")))
        if sanitized.source_url != source.source_url or sanitized.youtube_id != source.youtube_id:
            raise ValueError("Invalid source identity")
        return sanitized

    def opened(video_id: str) -> Record:
        try:
            library.ensure_conversation(video_id)
            video = library.get_video(video_id)
        except (KeyError, ValueError) as error:
            raise HTTPException(404, "影片不存在") from error
        if video["metadata"]:
            source = SourceMetadata.model_validate_json(str(video["metadata"]))
            metadata = source.model_dump(mode="json")
            metadata["selected_format"] = source.default_format_id
            metadata["selected_audio"] = source.default_audio_id
            metadata["above_1080"] = source.above_1080p
            metadata.update(quality_options(source))
            video["metadata"] = metadata
            formats = {item.id: item for item in source.formats}
        else:
            formats = {}
        video["assets"] = [
            {
                **{
                    key: value
                    for key, value in asset.items()
                    if key not in ("path", "checksum", "fingerprint")
                },
                "height": (
                    formats[str(asset["format_id"])].height
                    if str(asset["format_id"]) in formats
                    else None
                ),
            }
            for asset in library.assets(video_id)
        ]
        video["subtitles"] = library.subtitle_versions(video_id)
        video["previews"] = [
            {key: value for key, value in preview.items() if key not in ("path", "checksum")}
            for preview in library.previews(video_id)
        ]
        video["exports"] = public_exports(video_id)
        video["title"] = public_text(str(video["title"]), settings)
        return video

    def public_conversation(conversation_id: str) -> Record:
        conversation = library.get_conversation(conversation_id)
        messages = library.qa_messages(conversation_id)
        for message in messages:
            message["response"] = (
                json.loads(str(message["response_json"])) if message["response_json"] else None
            )
            message["memory_ids"] = json.loads(str(message["memory_ids"]))
            message["job_id"] = message["request_id"]
        conversation["messages"] = messages
        return conversation

    @app.get("/api/videos/{video_id}/conversations")
    def conversations(video_id: str) -> Record:
        try:
            library.ensure_conversation(video_id)
            video = library.get_video(video_id)
            return {
                "conversations": library.list_conversations(video_id),
                "current_conversation_id": video["last_conversation_id"],
            }
        except ValueError:
            raise HTTPException(404, "影片不存在") from None

    @app.post("/api/videos/{video_id}/conversations")
    def new_conversation(video_id: str, body: SelectionRequest) -> Record:
        if body.version_id is None:
            raise HTTPException(400, "請先取得／匯入完整字幕，並設為問答依據")
        try:
            created = library.create_conversation(video_id, body.version_id)
            return public_conversation(str(created["id"]))
        except ValueError:
            raise HTTPException(400, "請選擇本影片的完整字幕版本") from None

    @app.get("/api/conversations/{conversation_id}")
    def conversation(conversation_id: str) -> Record:
        try:
            return public_conversation(conversation_id)
        except ValueError:
            raise HTTPException(404, "對話不存在") from None

    @app.post("/api/videos/{video_id}/conversations/{conversation_id}/select")
    def select_conversation(video_id: str, conversation_id: str) -> Record:
        try:
            library.select_conversation(video_id, conversation_id)
            return public_conversation(conversation_id)
        except ValueError:
            raise HTTPException(404, "本影片的對話不存在") from None

    @app.delete("/api/conversations/{conversation_id}")
    def delete_conversation(conversation_id: str) -> dict[str, bool]:
        try:
            active = [
                str(message["request_id"])
                for message in library.qa_messages(conversation_id)
                if message["status"] == "pending"
            ]
            library.delete_conversation(conversation_id)
            # Tombstone is committed before signalling the worker's cancellation event.
            queue.signal(active)
            return {"deleted": True}
        except ValueError:
            raise HTTPException(404, "對話不存在") from None

    @app.post("/api/conversations/{conversation_id}/messages")
    def ask(conversation_id: str, body: QuestionRequest) -> Record:
        if settings.gemini_api_key is None:
            raise HTTPException(409, "請在專案 .env 設定 GEMINI_API_KEY 並重啟服務")
        try:
            message = qa.create(conversation_id, body.question)
            queue.enqueue(library.get_job(str(message["request_id"])))
            return message
        except ValueError:
            raise HTTPException(
                409, "對話不存在、正在回答，或問題無效；請先取得／匯入字幕"
            ) from None

    @app.get("/api/subtitles/{version_id}/cues")
    def source_cues(version_id: str) -> list[Record]:
        try:
            if not library.get_subtitle_version(version_id)["complete"]:
                raise ValueError("Incomplete source")
            return [cue.model_dump() for cue in library.subtitle_cues(version_id)]
        except ValueError:
            raise HTTPException(404, "完整字幕版本不存在") from None

    @app.post("/api/query")
    def query(body: QueryRequest) -> Record:
        try:
            youtube_id, url = parse_youtube_url(body.url)
            for video in library.list_videos():
                if video["youtube_id"] == youtube_id:
                    if body.refresh:
                        fresh = downloader.query(url)
                        fresh = safe_source(fresh)
                        library.refresh_metadata(str(video["id"]), fresh.model_dump_json())
                    return opened(str(video["id"]))
            source = downloader.query(url)
            source = safe_source(source)
            video = library.import_video(
                youtube_id, source.title, source.duration, url, source.model_dump_json()
            )
            return opened(str(video["id"]))
        except (ValueError, SourceError) as error:
            raise HTTPException(400, "來源不支援／無法取得") from error

    @app.get("/api/videos/{video_id}")
    def open_video(video_id: str) -> Record:
        return opened(video_id)

    @app.patch("/api/videos/{video_id}/position")
    def position(video_id: str, body: PositionRequest) -> dict[str, bool]:
        try:
            library.set_position(video_id, body.position)
        except (ValueError, KeyError) as error:
            raise HTTPException(400, "無效播放位置") from error
        return {"saved": True}

    @app.post("/api/videos/{video_id}/jobs")
    def start_download(video_id: str, body: JobRequest) -> Record:
        try:
            source = SourceMetadata.model_validate_json(
                str(library.get_video(video_id)["metadata"])
            )
            resolved = None
            if body.height is not None:
                resolved = resolve_source(source, body.height, body.subtitle_form)
                format_id, audio_id = resolved.format_id, resolved.audio_id
            elif body.format_id is not None and body.audio_id is not None:
                format_id, audio_id = body.format_id, body.audio_id
            else:
                raise ValueError("Missing resolution")
            if format_id not in {f.id for f in source.formats} or audio_id not in {
                a.id for a in source.audio_tracks
            }:
                raise ValueError("Invalid selection")
            job = library.create_job(video_id, format_id, audio_id)
            queue.enqueue(job)
            if resolved is not None:
                job = {**job, "resolved": resolved.model_dump(mode="json")}
            return job
        except (ValueError, KeyError) as error:
            raise HTTPException(400, "請選擇來源實際有的解析度") from error

    @app.get("/api/videos/{video_id}/subtitles")
    def subtitles(video_id: str) -> list[Record]:
        opened(video_id)
        return library.subtitle_versions(video_id)

    @app.post("/api/videos/{video_id}/subtitles/import")
    async def import_subtitles(
        video_id: str,
        request: Request,
        language: Annotated[str, Query(min_length=1, max_length=50)],
        name: Annotated[str, Query(min_length=1, max_length=200)],
        format: Literal["srt", "vtt"],
        parent_id: Annotated[str | None, Query(max_length=32)] = None,
    ) -> dict[str, object]:
        data = bytearray()
        async for chunk in request.stream():
            if len(data) + len(chunk) > 10 * 1024 * 1024:
                raise HTTPException(413, "字幕超過 10 MiB")
            data.extend(chunk)
        try:
            video = library.get_video(video_id)
            parsed = parse_subtitles(bytes(data), format, float(str(video["duration"])))
            cues = [
                Cue(id=cue.id, start=cue.start, end=cue.end, text=public_text(cue.text, settings))
                for cue in parsed.cues
            ]
            version = library.create_subtitle_version(
                video_id,
                public_text(language, settings),
                public_text(name, settings),
                "import",
                cues,
                parent_id=parent_id,
            )
            return {"version": version, "warnings": parsed.warnings}
        except (ValueError, KeyError) as error:
            raise HTTPException(400, public_text(str(error), settings)) from None

    def select(video_id: str, selection: str, body: SelectionRequest) -> dict[str, bool]:
        try:
            library.set_subtitle_selection(video_id, selection, body.version_id)
            return {"saved": True}
        except ValueError:
            raise HTTPException(400, "請選擇本影片的完整字幕版本") from None

    @app.post("/api/videos/{video_id}/subtitles/playback")
    def playback_selection(video_id: str, body: SelectionRequest) -> dict[str, bool]:
        return select(video_id, "playback", body)

    @app.post("/api/videos/{video_id}/subtitles/translation-source")
    def translation_selection(video_id: str, body: SelectionRequest) -> dict[str, bool]:
        return select(video_id, "translation_source", body)

    @app.post("/api/videos/{video_id}/subtitles/export-selection")
    def export_selection(video_id: str, body: SelectionRequest) -> dict[str, bool]:
        return select(video_id, "export", body)

    @app.post("/api/videos/{video_id}/subtitles/acquire")
    def acquire(video_id: str, body: AcquisitionRequest) -> Record:
        try:
            job = acquisition.create(video_id, body.asset_id, body.language, body.source_type)
            queue.enqueue(job)
            return job
        except ValueError:
            raise HTTPException(400, "請選擇已保存影音與有效平台字幕") from None

    @app.post("/api/videos/{video_id}/translations")
    def translate(video_id: str, body: TranslationRequest) -> Record:
        if settings.gemini_api_key is None:
            raise HTTPException(409, "請在專案 .env 設定 GEMINI_API_KEY 並重啟服務")
        try:
            source_id = library.get_video(video_id)["translation_source_version_id"]
            if source_id is None:
                raise ValueError("Select source")
            job = translation.create(
                video_id,
                str(source_id),
                public_text(body.language, settings),
                regenerate=body.regenerate,
            )
            queue.enqueue(job)
            return job
        except ValueError:
            raise HTTPException(400, "請先以完整版本設定翻譯來源及有效目標語系") from None

    @app.get("/api/subtitles/{version_id}/track.vtt")
    def track(version_id: str) -> Response:
        return subtitle_response(version_id, "vtt", False)

    @app.get("/api/subtitles/{version_id}/download.{format}")
    def subtitle_download(version_id: str, format: str) -> Response:
        return subtitle_response(version_id, format, True)

    def subtitle_response(version_id: str, format: str, download: bool) -> Response:
        try:
            version = library.get_subtitle_version(version_id)
            if not version["complete"] or format not in {"srt", "vtt"}:
                raise ValueError("Incomplete subtitle")
            payload = render_subtitles(library.subtitle_cues(version_id), format)
            headers = (
                {"Content-Disposition": f'attachment; filename="{version_id}.{format}"'}
                if download
                else {}
            )
            return Response(
                payload,
                media_type="text/vtt" if format == "vtt" else "application/x-subrip",
                headers=headers,
            )
        except ValueError:
            raise HTTPException(404, "字幕不存在或尚未完整") from None

    @app.post("/api/videos/{video_id}/exports/preview")
    def preview_export(video_id: str, body: ExportRequest) -> dict[str, object]:
        try:
            source_id = library.get_video(video_id)["translation_source_version_id"]
            return exporter.preview(
                video_id,
                body.asset_id,
                body.target_version_ids,
                body.include_original,
                body.original_version_id,
                str(source_id) if source_id else None,
                body.container,
                body.subtitle_form,
            )
        except SourceError as error:
            if error.code == "hardware_encoder_unavailable":
                raise HTTPException(
                    409, "此 Mac 無法使用 VideoToolbox 硬體編碼，無法燒錄字幕（不改用軟體編碼）"
                ) from None
            raise HTTPException(400, "請選擇有效影音及完整字幕，並確認本機 ffmpeg 可用") from None
        except ValueError:
            raise HTTPException(400, "請選擇有效影音及完整字幕，並確認本機 ffmpeg 可用") from None

    @app.post("/api/videos/{video_id}/exports")
    def start_export(video_id: str, body: ConfirmExportRequest) -> Record:
        try:
            job = exporter.create(video_id, body.snapshot)
            queue.enqueue(job)
            return job
        except (ValueError, SourceError):
            raise HTTPException(400, "匯出摘要無效，請重新確認格式與字幕軌") from None

    def public_exports(video_id: str) -> list[Record]:
        return [
            {key: value for key, value in artifact.items() if key != "path"}
            for artifact in library.exports(video_id)
        ]

    @app.get("/api/exports/{export_id}/download")
    def export_download(export_id: str) -> FileResponse:
        try:
            artifact = library.get_export(export_id)
            relative = Path(str(artifact["path"]))
            with library._directory(relative.parent):
                path = library.root / relative
                if path.is_symlink() or not path.is_file():
                    raise ValueError("Missing export")
            return FileResponse(
                path,
                filename=path.name,
                media_type="video/mp4" if artifact["container"] == "mp4" else "video/x-matroska",
            )
        except (ValueError, OSError):
            raise HTTPException(404, "成品尚未驗證或不存在") from None

    @app.post("/api/videos/{video_id}/previews")
    def make_preview(video_id: str, body: PreviewRequest) -> Record:
        try:
            job = previews.create(video_id, body.asset_id)
            queue.enqueue(job)
            return job
        except ValueError:
            raise HTTPException(
                409, "此影音可直接播放、已有預覽或影片不存在；如需重做請先清除預覽"
            ) from None

    @app.delete("/api/videos/{video_id}/previews")
    def clear_previews(video_id: str) -> dict[str, int]:
        try:
            result = library.clear_previews(video_id)
        except ValueError:
            raise HTTPException(404, "影片不存在") from None
        except OSError:
            raise HTTPException(500, "部分預覽檔案未能刪除，請稍後再按清除預覽") from None
        cancelled = [str(job_id) for job_id in cast(list[object], result["cancelled_jobs"])]
        queue.signal(cancelled)
        return {"removed": int(str(result["removed"])), "cancelled_jobs": len(cancelled)}

    @app.get("/api/previews/{preview_id}/media")
    def preview_media(preview_id: str) -> FileResponse:
        try:
            preview = library.get_preview(preview_id)
            relative = Path(str(preview["path"]))
            with library._directory(relative.parent):
                path = library.root / relative
                if path.is_symlink() or not path.is_file():
                    raise ValueError("Missing preview")
            return FileResponse(path, media_type="video/mp4")
        except (ValueError, OSError):
            raise HTTPException(404, "預覽不存在") from None

    @app.get("/api/videos/{video_id}/deletion")
    def deletion_scope(video_id: str) -> Record:
        try:
            scope = library.deletion_scope(video_id)
        except (ValueError, OSError):
            raise HTTPException(404, "影片不存在") from None
        scope["title"] = public_text(str(scope["title"]), settings)
        return scope

    def purge(video_id: str, active: list[str]) -> dict[str, bool]:
        queue.signal(active)
        if not queue.wait_media_stopped(active, DELETE_WAIT_SECONDS):
            raise HTTPException(409, "仍有工作正在停止；影片已標記待清理，請稍後按重試刪除")
        try:
            library.purge_video(video_id)
        except OSError:
            raise HTTPException(500, "刪除未完成；影片已標記待清理，請按重試刪除") from None
        return {"deleted": True}

    @app.post("/api/videos/{video_id}/delete")
    def delete_video(video_id: str, body: DeleteRequest) -> dict[str, bool]:
        try:
            scope = library.deletion_scope(video_id)
        except (ValueError, OSError):
            raise HTTPException(404, "影片不存在") from None
        if body.confirmation != scope["confirmation"]:
            raise HTTPException(409, "刪除範圍已變更或尚未確認，請重新檢視範圍後再確認")
        return purge(video_id, library.mark_deleting(video_id))

    @app.get("/api/deletions")
    def pending_deletions() -> list[Record]:
        return [
            {**item, "title": public_text(str(item["title"]), settings)}
            for item in library.pending_deletions()
        ]

    @app.post("/api/deletions/{video_id}/retry")
    def retry_deletion(video_id: str) -> dict[str, bool]:
        if video_id not in {str(item["id"]) for item in library.pending_deletions()}:
            raise HTTPException(404, "沒有待清理的影片")
        active = [str(job["id"]) for job in library.list_jobs() if job["video_id"] == video_id]
        return purge(video_id, active)

    @app.get("/api/jobs")
    def jobs() -> list[Record]:
        return library.list_jobs()

    @app.get("/api/jobs/{job_id}")
    def job_status(job_id: str) -> Record:
        try:
            return library.get_job(job_id)
        except ValueError as error:
            raise HTTPException(404, "工作不存在") from error

    @app.post("/api/jobs/{job_id}/cancel")
    def cancel_job(job_id: str) -> Record:
        try:
            queue.cancel(job_id)
            return library.get_job(job_id)
        except (KeyError, ValueError) as error:
            raise HTTPException(404, "工作不存在") from error

    @app.post("/api/jobs/{job_id}/retry")
    def retry_job(job_id: str) -> Record:
        try:
            previous = library.get_job(job_id)
            if previous["kind"] in {"translation", "qa"} and settings.gemini_api_key is None:
                raise HTTPException(409, "請在專案 .env 設定 GEMINI_API_KEY 並重啟服務")
            if previous["error_code"] == "container_confirmation_required":
                raise HTTPException(409, "請改選 MKV，重新確認摘要並建立新的匯出工作")
            library.retry_job(job_id)
            job = library.get_job(job_id)
            queue.enqueue(job)
            return job
        except (ValueError, KeyError) as error:
            raise HTTPException(409, "工作無法重試") from error

    @app.get("/api/assets/{asset_id}/media")
    def media(asset_id: str) -> FileResponse:
        try:
            asset = library.get_asset(asset_id)
            relative = Path(str(asset["path"]))
            with library._directory(relative.parent):
                path = library.root / relative
                if path.is_symlink() or not path.is_file():
                    raise ValueError("Missing media")
            return FileResponse(
                path, media_type="video/mp4" if asset["container"] == "mp4" else "video/x-matroska"
            )
        except (KeyError, ValueError, OSError) as error:
            raise HTTPException(404, "媒體不存在") from error

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(static / "index.html")

    app.mount("/static", StaticFiles(directory=static), name="static")
    return app
