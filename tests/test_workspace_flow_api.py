"""One-click flow: the confirm screen choices, the chained stages and the finished artifact."""

import json
import time
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tests.test_workspace_burned_export import FakeFFmpeg
from tests.test_workspace_exports import setup_service
from tests.test_workspace_s2_api import FakeAdapter
from tests.test_workspace_s3_api import ORIGIN
from video_content_capture.workspace.app import create_app
from video_content_capture.workspace.config import load_settings
from video_content_capture.workspace.exports import MediaExporter
from video_content_capture.workspace.flows import (
    BUSY_MESSAGE,
    MISSING_KEY_CODE,
    MISSING_KEY_MESSAGE,
)
from video_content_capture.workspace.storage import Library
from video_content_capture.workspace.subtitles import Cue
from video_content_capture.workspace.translation import TranslationResponse
from video_content_capture.workspace.youtube import SourceError, SourceMetadata, parse_metadata

VTT = b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\nHello world\n"


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "VCC_LIBRARY_DIR"):
        monkeypatch.delenv(name, raising=False)


class FakeASR:
    """The flow must reuse the platform subtitle; local recognition would be a defect here."""

    def transcribe(self, path: Path, language: str | None, duration: float, cancel: Event):
        raise AssertionError("Local recognition must not run when a platform track exists")


class FlowAdapter(FakeAdapter):
    """FakeAdapter plus the original language and exactly one platform subtitle track."""

    def __init__(self, language: str = "en", automatic: bool = False) -> None:
        super().__init__()
        self.source = parse_metadata(
            {
                "id": "abcdefghijk",
                "title": "離線影片",
                "duration": 10,
                "language": language,
                "automatic_captions" if automatic else "subtitles": {language: [{"ext": "vtt"}]},
                "formats": [
                    {"format_id": "v", "height": 720, "vcodec": "avc1", "acodec": "none"},
                    {"format_id": "a", "vcodec": "none", "acodec": "mp4a"},
                ],
            },
            "https://youtu.be/abcdefghijk",
        )
        self.subtitles_downloaded = 0
        # Set to an exception to fail the subtitle stage; cleared to let a retry succeed.
        self.subtitle_failure: Exception | None = None

    def download_subtitle(self, source: SourceMetadata, track, cancel: Event) -> bytes:
        self.subtitles_downloaded += 1
        if self.subtitle_failure is not None:
            raise self.subtitle_failure
        return VTT


class BlockingAdapter(FlowAdapter):
    """Holds the download open so a running flow can be observed and edited against."""

    def __init__(self, language: str = "en", automatic: bool = False) -> None:
        super().__init__(language, automatic)
        self.entered, self.release = Event(), Event()

    def download(self, source, format_id, audio_id, directory, cancel, progress, stages=None):
        progress("downloading", 1, 2)
        self.entered.set()
        assert self.release.wait(5)
        return super().download(source, format_id, audio_id, directory, cancel, progress, stages)


class HoldingAdapter(FlowAdapter):
    """Blocks the first download only, so an unrelated job can park in the lane."""

    def __init__(self, language: str = "en") -> None:
        super().__init__(language)
        self.entered, self.release = Event(), Event()
        self.hold = True

    def download(self, source, format_id, audio_id, directory, cancel, progress, stages=None):
        if self.hold:
            self.hold = False
            progress("downloading", 1, 2)
            self.entered.set()
            assert self.release.wait(5)
        return super().download(source, format_id, audio_id, directory, cancel, progress, stages)


class FakeTranslation:
    def __init__(self) -> None:
        # Counted so a retry can prove it reused a finished translation instead of redoing it.
        self.calls = 0

    def translate(
        self, key: SecretStr, model: str, language: str, cues: list[Cue]
    ) -> TranslationResponse:
        self.calls += 1
        return TranslationResponse(
            json.dumps({"cues": [{"id": cue.id, "text": "翻譯"} for cue in cues]}), "STOP"
        )


def start_flow(client: TestClient, video_id: str, target_language: str, **choices: object):
    body: dict[str, object] = {"height": 720, "target_language": target_language}
    body.update(choices)
    return client.post(f"/api/videos/{video_id}/flows", json=body, headers=ORIGIN)


def confirm(client: TestClient, video_id: str, **params: object):
    return client.get(f"/api/videos/{video_id}/flow", params=params)


def wait_for_flow(client: TestClient, flow_id: str, expect: str = "completed") -> dict:
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        payload = client.get(f"/api/flows/{flow_id}").json()
        if payload["flow"]["status"] != "running":
            assert payload["flow"]["status"] == expect, payload
            return payload
        time.sleep(0.01)
    pytest.fail("One-click flow did not finish within 10 seconds")


@pytest.mark.parametrize(("automatic", "label"), [(False, "人工"), (True, "自動")])
def test_confirm_reports_the_plan_labels_and_key_block(
    tmp_path: Path, automatic: bool, label: str
) -> None:
    adapter = FlowAdapter("en", automatic)
    with TestClient(
        create_app(load_settings(tmp_path), adapter, asr_adapter=FakeASR()),
        base_url="http://127.0.0.1:8765",
    ) as client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        plan = confirm(client, video["id"], height=720, target_language="zh-TW").json()
        assert plan["title"] == "離線影片"
        assert plan["duration"] == 10
        assert plan["resolutions"] == [720]
        assert plan["resolution"] == plan["default_resolution"] == 720
        assert plan["audio"]["id"] == "a"
        assert plan["original_language"] == "en"
        assert plan["original_source"] == ("platform_auto" if automatic else "platform_manual")
        assert plan["original_source_label"] == label
        assert plan["subtitle_form"] == "burned"
        assert plan["output_format"] == "mp4"
        assert plan["steps"] == ["download", "subtitles", "translation", "export"]
        assert plan["needs_translation"] is True
        assert plan["gemini_configured"] is False
        assert plan["blocked_reason"] == MISSING_KEY_CODE
        assert plan["busy"] is False
        same = confirm(client, video["id"], target_language="en").json()
        assert same["steps"] == ["download", "subtitles", "export"]
        assert same["needs_translation"] is False
        assert same["blocked_reason"] is None
        assert confirm(client, video["id"], target_language="not a language").status_code == 400
        assert confirm(client, video["id"], target_language="zh-TW", height=2160).status_code == 400
        assert confirm(client, "missing-video").status_code == 404
        assert client.get("/api/flows/missing").status_code == 404


def test_regional_chinese_targets_are_different_languages(tmp_path: Path) -> None:
    adapter = FlowAdapter("zh-TW")
    with TestClient(
        create_app(load_settings(tmp_path), adapter, asr_adapter=FakeASR()),
        base_url="http://127.0.0.1:8765",
    ) as client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        same = confirm(client, video["id"], target_language="zh-TW").json()
        assert same["needs_translation"] is False
        assert same["steps"] == ["download", "subtitles", "export"]
        assert same["blocked_reason"] is None
        other = confirm(client, video["id"], target_language="zh-CN").json()
        assert other["needs_translation"] is True
        assert other["steps"] == ["download", "subtitles", "translation", "export"]
        assert other["blocked_reason"] == MISSING_KEY_CODE
        blocked = start_flow(client, video["id"], "zh-CN")
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == MISSING_KEY_MESSAGE
        assert start_flow(client, video["id"], "not a language").status_code == 400
        assert start_flow(client, video["id"], "zh-TW", height=2160).status_code == 400
        assert client.get("/api/jobs").json() == []
        assert len(client.get(f"/api/videos/{video['id']}/subtitles").json()) == 0


def test_flow_chains_every_stage_and_exposes_the_finished_artifact(tmp_path: Path) -> None:
    runner = FakeFFmpeg()
    adapter = FlowAdapter("en")
    settings = replace(load_settings(tmp_path), gemini_api_key=SecretStr("fake-api-sentinel"))
    with TestClient(
        create_app(
            settings,
            adapter,
            translation_adapter=FakeTranslation(),
            asr_adapter=FakeASR(),
            media_exporter=MediaExporter(runner),
        ),
        base_url="http://127.0.0.1:8765",
    ) as client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        base = f"/api/videos/{video['id']}"
        started = start_flow(client, video["id"], "zh-TW")
        assert started.status_code == 200, started.text
        payload = started.json()
        flow_id = payload["flow"]["id"]
        assert payload["job"]["kind"] == "download"
        assert payload["snapshot"]["subtitle_form"] == "burned"
        assert payload["snapshot"]["translate"] is True
        assert payload["snapshot"]["include_original"] is False
        assert [stage["stage"] for stage in payload["stages"]] == [
            "download",
            "subtitles",
            "translation",
            "export",
        ]
        # The worker may already have picked the download up, so only the plan is asserted here.
        assert payload["stages"][0]["job_id"] == payload["job"]["id"]
        status = wait_for_flow(client, flow_id)
        assert status["snapshot"] == payload["snapshot"]
        assert {stage["status"] for stage in status["stages"]} == {"completed"}
        assert [stage["job_id"] for stage in status["stages"]] == [
            job["id"] for job in client.get("/api/jobs").json()
        ]
        artifact = status["artifact"]
        assert artifact is not None and "path" not in artifact
        assert artifact["job_id"] == status["stages"][-1]["job_id"]
        assert artifact["container"] == "mp4"
        assert artifact["subtitle_form"] == "burned"
        summary = json.loads(artifact["summary"])
        assert summary["include_original"] is False
        versions = client.get(base + "/subtitles").json()
        source = next(
            version for version in versions if version["source_type"] == "platform_manual"
        )
        target = next(version for version in versions if version["source_type"] == "translation")
        assert source["complete"] == 1
        assert target["complete"] == 1 and target["parent_id"] == source["id"]
        assert target["language"] == "zh-TW"
        assert summary["tracks"][0]["version_id"] == target["id"]
        assert adapter.subtitles_downloaded == 1
        download = client.get(f"/api/exports/{artifact['id']}/download")
        assert download.status_code == 200
        assert download.content == b"burned export"


def test_same_language_target_skips_translation_without_a_key(tmp_path: Path) -> None:
    runner = FakeFFmpeg()
    adapter = FlowAdapter("zh-TW")
    with TestClient(
        create_app(
            load_settings(tmp_path),
            adapter,
            asr_adapter=FakeASR(),
            media_exporter=MediaExporter(runner),
        ),
        base_url="http://127.0.0.1:8765",
    ) as client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        started = start_flow(client, video["id"], "zh-TW")
        assert started.status_code == 200, started.text
        assert started.json()["snapshot"]["translate"] is False
        status = wait_for_flow(client, started.json()["flow"]["id"])
        assert [stage["stage"] for stage in status["stages"]] == [
            "download",
            "subtitles",
            "export",
        ]
        assert {stage["status"] for stage in status["stages"]} == {"completed"}
        assert [job["kind"] for job in client.get("/api/jobs").json()] == [
            "download",
            "subtitles",
            "export",
        ]
        versions = client.get(f"/api/videos/{video['id']}/subtitles").json()
        assert [version["source_type"] for version in versions] == ["platform_manual"]
        # The acquired original already is the target language, so the export reuses it.
        summary = json.loads(status["artifact"]["summary"])
        assert summary["tracks"][0]["version_id"] == versions[0]["id"]


def test_missing_gemini_key_blocks_before_anything_starts(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    adapter = FlowAdapter("en")
    settings = replace(load_settings(tmp_path), library_dir=library.root)
    with TestClient(
        create_app(
            settings,
            adapter,
            asr_adapter=FakeASR(),
            media_exporter=MediaExporter(FakeFFmpeg()),
        ),
        base_url="http://127.0.0.1:8765",
    ) as client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        payload = confirm(client, video["id"], target_language="zh-TW").json()
        assert payload["gemini_configured"] is False
        blocked = start_flow(client, video["id"], "zh-TW")
        assert blocked.status_code == 409
        assert blocked.json()["detail"] == MISSING_KEY_MESSAGE
        assert library.flows(video["id"]) == []
        assert library.list_jobs() == []
        assert library.subtitle_versions(video["id"]) == []
        # A same-language target needs no translation, so the very same choices are accepted.
        accepted = start_flow(client, video["id"], "en")
        assert accepted.status_code == 200, accepted.text
        status = wait_for_flow(client, accepted.json()["flow"]["id"])
        assert [stage["stage"] for stage in status["stages"]] == ["download", "subtitles", "export"]


def test_duplicate_start_is_conflicted_while_the_flow_runs(tmp_path: Path) -> None:
    runner = FakeFFmpeg()
    adapter = BlockingAdapter("en")
    settings = replace(load_settings(tmp_path), gemini_api_key=SecretStr("fake-api-sentinel"))
    with TestClient(
        create_app(
            settings,
            adapter,
            translation_adapter=FakeTranslation(),
            asr_adapter=FakeASR(),
            media_exporter=MediaExporter(runner),
        ),
        base_url="http://127.0.0.1:8765",
    ) as client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        first = start_flow(client, video["id"], "zh-TW")
        assert first.status_code == 200, first.text
        flow_id = first.json()["flow"]["id"]
        assert adapter.entered.wait(5)
        try:
            running = client.get(f"/api/flows/{flow_id}").json()
            assert running["flow"]["status"] == "running"
            assert running["stages"][0] == {
                "stage": "download",
                "status": "running",
                "detail": "downloading",
                "progress": 0.5,
                "error_code": None,
                "job_id": first.json()["job"]["id"],
            }
            assert [stage["status"] for stage in running["stages"][1:]] == ["pending"] * 3
            assert running["artifact"] is None
            assert confirm(client, video["id"], target_language="zh-TW").json()["busy"] is True
            duplicate = start_flow(client, video["id"], "zh-TW")
            assert duplicate.status_code == 409
            assert duplicate.json()["detail"] == BUSY_MESSAGE
        finally:
            adapter.release.set()
        assert wait_for_flow(client, flow_id)["flow"]["status"] == "completed"
        assert confirm(client, video["id"], target_language="zh-TW").json()["busy"] is False
        second = start_flow(client, video["id"], "zh-TW")
        assert second.status_code == 200, second.text
        assert second.json()["flow"]["id"] != flow_id
        assert wait_for_flow(client, second.json()["flow"]["id"])["flow"]["status"] == "completed"


def test_export_keeps_the_frozen_choices_and_threads_include_original(tmp_path: Path) -> None:
    library, video_id, original, _, exporter, _ = setup_service(tmp_path)
    adapter = BlockingAdapter("en")
    library.refresh_metadata(video_id, adapter.source.model_dump_json())
    other = library.create_subtitle_version(
        video_id, "ja", "Japanese", "import", [Cue(id="one", start=0, end=0.5, text="ja")]
    )
    library.set_subtitle_selection(video_id, "translation_source", str(original["id"]))
    settings = replace(
        load_settings(tmp_path), library_dir=library.root, gemini_api_key=SecretStr("fake-key")
    )
    with TestClient(
        create_app(
            settings,
            adapter,
            translation_adapter=FakeTranslation(),
            asr_adapter=FakeASR(),
            media_exporter=exporter,
        ),
        base_url="http://127.0.0.1:8765",
    ) as client:
        base = f"/api/videos/{video_id}"
        started = start_flow(
            client, video_id, "zh-TW", include_original=True, subtitle_form="tracks"
        )
        assert started.status_code == 200, started.text
        flow_id = started.json()["flow"]["id"]
        assert started.json()["snapshot"]["source_version_id"] == original["id"]
        assert adapter.entered.wait(5)
        try:
            # Later UI edits must not change what the running flow does.
            for selection in ("translation-source", "export-selection"):
                assert (
                    client.post(
                        base + f"/subtitles/{selection}",
                        json={"version_id": other["id"]},
                        headers=ORIGIN,
                    ).status_code
                    == 200
                )
        finally:
            adapter.release.set()
        status = wait_for_flow(client, flow_id)
        assert status["snapshot"] == started.json()["snapshot"]
        assert status["snapshot"]["include_original"] is True
        assert status["snapshot"]["subtitle_form"] == "tracks"
        translated = next(
            version
            for version in library.subtitle_versions(video_id)
            if version["source_type"] == "translation"
        )
        assert translated["parent_id"] == original["id"]
        summary = json.loads(status["artifact"]["summary"])
        assert summary["include_original"] is True
        assert summary["source_version_id"] == original["id"]
        assert summary["original_version_id"] == original["id"]
        assert [track["version_id"] for track in summary["tracks"]] == [
            translated["id"],
            original["id"],
        ]
        assert [track.version_id for track in exporter.tracks] == [translated["id"], original["id"]]
        # The edit still belongs to the video; only the frozen flow kept its own source.
        assert library.get_video(video_id)["translation_source_version_id"] == other["id"]


def test_flow_reclaims_a_queued_download_left_unlinked_by_a_crash(tmp_path: Path) -> None:
    """A crash between `create_job` and the worker leaves a queued job with no flow.

    `create_job` dedupes on (video, format, audio), so the next flow reuses that job.
    Returning it unlinked made `on_job_finished` ignore its own download and the flow
    never left `running`: every later start was answered with 409 and nothing could
    end it. The reused job has to adopt the caller's flow.
    """
    library = Library(tmp_path / "library")
    library.initialize()
    adapter = FlowAdapter("en")
    settings = replace(load_settings(tmp_path), library_dir=library.root)
    with TestClient(
        create_app(
            settings,
            adapter,
            asr_adapter=FakeASR(),
            media_exporter=MediaExporter(FakeFFmpeg()),
        ),
        base_url="http://127.0.0.1:8765",
    ) as client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        crashed = library.create_flow(
            video["id"], "download", {"height": 720, "target_language": "en"}
        )
        library.update_flow(str(crashed["id"]), status="interrupted")
        orphan = library.create_job(video["id"], "v", "a")
        assert orphan["flow_id"] is None and orphan["status"] == "queued"

        started = start_flow(client, video["id"], "en")
        assert started.status_code == 200, started.text
        flow_id = started.json()["flow"]["id"]
        assert started.json()["job"]["id"] == orphan["id"]
        assert library.get_job(str(orphan["id"]))["flow_id"] == flow_id
        status = wait_for_flow(client, flow_id)
        assert {stage["status"] for stage in status["stages"]} == {"completed"}
        assert confirm(client, video["id"], target_language="en").json()["busy"] is False


def test_cancelling_a_queued_stage_ends_the_flow_instead_of_wedging_it(tmp_path: Path) -> None:
    """A stage cancelled while still queued never reaches the worker.

    `MediaQueue.cancel` drops it from `pending`, so `_worker` never reports it finished and
    the flow stayed `running` forever: `confirm` said busy, every later start was answered
    with 409, and no endpoint could end it. Cancelling the stage has to end the flow.
    """
    adapter = HoldingAdapter()
    library = Library(tmp_path / "library")
    library.initialize()
    settings = replace(load_settings(tmp_path), library_dir=library.root)
    with TestClient(
        create_app(
            settings,
            adapter,
            asr_adapter=FakeASR(),
            media_exporter=MediaExporter(FakeFFmpeg()),
        ),
        base_url="http://127.0.0.1:8765",
    ) as client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        # An unrelated download holds the lane, so the flow's first stage stays `queued`.
        other = library.import_video(
            "lmnopqrstuv",
            "第二部",
            10,
            "https://youtu.be/lmnopqrstuv",
            adapter.source.model_copy(update={"youtube_id": "lmnopqrstuv"}).model_dump_json(),
        )
        blocking = client.post(
            f"/api/videos/{other['id']}/jobs",
            json={"format_id": "v", "audio_id": "a"},
            headers=ORIGIN,
        ).json()
        assert adapter.entered.wait(5)
        try:
            started = start_flow(client, video["id"], "en")
            assert started.status_code == 200, started.text
            flow_id = started.json()["flow"]["id"]
            stage = started.json()["job"]
            assert stage["id"] != blocking["id"] and stage["status"] == "queued"
            assert confirm(client, video["id"], target_language="en").json()["busy"] is True

            cancelled = client.post(f"/api/jobs/{stage['id']}/cancel", headers=ORIGIN)
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["status"] == "cancelled"

            flow = client.get(f"/api/flows/{flow_id}").json()
            assert flow["flow"]["status"] == "cancelled", flow
            assert flow["flow"]["error_code"] == "cancelled"
            assert [step["stage"] for step in flow["stages"]] == ["download", "subtitles", "export"]
            assert [step["status"] for step in flow["stages"]] == [
                "cancelled",
                "pending",
                "pending",
            ]
            assert library.active_flow(video["id"]) is None
            assert confirm(client, video["id"], target_language="en").json()["busy"] is False

            restarted = start_flow(client, video["id"], "en")
            assert restarted.status_code == 200, restarted.text
            assert restarted.json()["flow"]["id"] != flow_id
        finally:
            adapter.release.set()


def make_flow_client(
    tmp_path: Path,
    adapter: FlowAdapter,
    runner: FakeFFmpeg,
    translation: FakeTranslation | None = None,
):
    """A client with a Gemini key so a translating flow can be driven end to end."""
    library = Library(tmp_path / "library")
    library.initialize()
    settings = replace(
        load_settings(tmp_path, library_dir=library.root),
        gemini_api_key=SecretStr("fake-api-sentinel"),
    )
    client = TestClient(
        create_app(
            settings,
            adapter,
            translation_adapter=translation or FakeTranslation(),
            asr_adapter=FakeASR(),
            media_exporter=MediaExporter(runner),
        ),
        base_url="http://127.0.0.1:8765",
    )
    return client, library


def _flow_id(library: Library, flow_id: str) -> str:
    """The stored id, typed: `Record` values are `object` under mypy strict."""
    return str(library.get_flow(flow_id)["id"])


def test_rejected_retry_and_cancel_leave_the_flow_untouched(tmp_path: Path) -> None:
    """Cancelling a queued stage is enough; retrying a live flow must change nothing."""
    adapter = HoldingAdapter()
    client, library = make_flow_client(tmp_path, adapter, FakeFFmpeg())
    with client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        # The fake adapter answers the same metadata for any URL, so the second video has
        # to be imported directly; that is the pattern the existing cancel test uses.
        other = library.import_video(
            "lmnopqrstuv",
            "第二部",
            10,
            "https://youtu.be/lmnopqrstuv",
            adapter.source.model_copy(update={"youtube_id": "lmnopqrstuv"}).model_dump_json(),
        )
        blocking = client.post(
            f"/api/videos/{other['id']}/jobs",
            json={"format_id": "v", "audio_id": "a"},
            headers=ORIGIN,
        ).json()
        assert adapter.entered.wait(5)
        try:
            started = start_flow(client, video["id"], "en")
            assert started.status_code == 200, started.text
            created = _flow_id(library, started.json()["flow"]["id"])
            stage_id = str(started.json()["job"]["id"])
            assert stage_id != blocking["id"]
            assert library.get_job(stage_id)["status"] == "queued"

            # Retrying a live flow is a client error, and the row is left alone.
            retried = client.post(f"/api/flows/{created}/retry", headers=ORIGIN)
            assert retried.status_code == 409, retried.text
            assert library.get_flow(created)["status"] == "running"

            unknown = client.post("/api/flows/0123456789abcdef/cancel", headers=ORIGIN)
            assert unknown.status_code == 404

            cancelled = client.post(f"/api/flows/{created}/cancel", headers=ORIGIN)
            assert cancelled.status_code == 200, cancelled.text
            assert cancelled.json()["flow"]["status"] == "cancelled"
            assert cancelled.json()["flow"]["error_code"] == "cancelled"
            assert library.get_job(stage_id)["status"] == "cancelled"

            # Cancelling is final; the flow may still be resumed from its first stage.
            assert client.post(f"/api/flows/{created}/cancel", headers=ORIGIN).status_code == 409
            resumed = client.post(f"/api/flows/{created}/retry", headers=ORIGIN)
            assert resumed.status_code == 200, resumed.text
            assert resumed.json()["flow"]["status"] == "running"
        finally:
            adapter.release.set()


def test_retry_resumes_the_failed_stage_without_downloading_again(tmp_path: Path) -> None:
    """A subtitle stage that fails must not restart the download it already published."""
    adapter = FlowAdapter("en")
    adapter.subtitle_failure = SourceError("subtitle_unavailable", "字幕已消失")
    client, library = make_flow_client(tmp_path, adapter, FakeFFmpeg())
    with client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        started = start_flow(client, video["id"], "zh-TW")
        assert started.status_code == 200, started.text
        flow_id = _flow_id(library, started.json()["flow"]["id"])
        status = wait_for_flow(client, flow_id, expect="failed")
        assert status["flow"]["stage"] == "subtitles"
        assert status["flow"]["error_code"] == "subtitle_unavailable"
        assert [stage["status"] for stage in status["stages"]] == [
            "completed",
            "failed",
            "pending",
            "pending",
        ]
        assert status["stages"][1]["error_code"] == "subtitle_unavailable"
        assert adapter.downloads == 1

        adapter.subtitle_failure = None
        resumed = client.post(f"/api/flows/{flow_id}/retry", headers=ORIGIN)
        assert resumed.status_code == 200, resumed.text
        assert resumed.json()["flow"]["id"] == flow_id
        finished = wait_for_flow(client, flow_id)
        assert {stage["status"] for stage in finished["stages"]} == {"completed"}
        # The retry resumed at the subtitle stage; the published download was reused.
        assert adapter.downloads == 1
        listed = confirm(client, video["id"], target_language="zh-TW").json()
        assert listed["latest_flow_id"] == flow_id


def test_retry_after_a_chaining_failure_redoes_only_the_chain(tmp_path: Path) -> None:
    """A stage that succeeded while the next job could not be built is retryable as is.

    `FakeFFmpeg(hardware=False)` refuses the VideoToolbox probe, and the burned export probes
    it while *building* the export job — inside `FlowService._export_job`, which runs inside
    `advance()`. So the translation stage completes and the flow dies in chaining with
    `error_code == "flow_failed"`, `stage` still `translation` and no export job at all. That
    is exactly the `completed`-stage branch of spec §5.2 step 6.
    """
    runner = FakeFFmpeg()
    runner.hardware = False
    translation = FakeTranslation()
    adapter = FlowAdapter("en")
    client, library = make_flow_client(tmp_path, adapter, runner, translation)
    with client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        started = start_flow(client, video["id"], "zh-TW")
        flow_id = _flow_id(library, started.json()["flow"]["id"])
        status = wait_for_flow(client, flow_id, expect="failed")
        assert status["flow"]["stage"] == "translation"
        assert status["flow"]["error_code"] == "flow_failed"
        assert translation.calls == 1
        translated = next(job for job in library.flow_jobs(flow_id) if job["kind"] == "translation")
        translated_id, attempt = str(translated["id"]), str(translated["attempt_id"])
        assert translated["status"] == "completed"
        # The chain died while building the export job, so that job was never created.
        assert [job["kind"] for job in library.flow_jobs(flow_id)] == [
            "download",
            "subtitles",
            "translation",
        ]
        published = [asset["id"] for asset in library.assets(video["id"])]
        assert published

        runner.hardware = True
        resumed = client.post(f"/api/flows/{flow_id}/retry", headers=ORIGIN)
        assert resumed.status_code == 200, resumed.text
        finished = wait_for_flow(client, flow_id)
        assert finished["artifact"] is not None
        # Not one earlier stage ran again: same counts, same assets, same translation attempt.
        assert adapter.downloads == 1
        assert adapter.subtitles_downloaded == 1
        assert translation.calls == 1
        assert [asset["id"] for asset in library.assets(video["id"])] == published
        again = library.get_job(translated_id)
        assert again["attempt_id"] == attempt
        assert again["status"] == "completed"
        assert [job["kind"] for job in library.flow_jobs(flow_id)] == [
            "download",
            "subtitles",
            "translation",
            "export",
        ]


def test_flow_routes_report_unknown_flows_and_refuse_a_finished_one(tmp_path: Path) -> None:
    adapter = FlowAdapter("en")
    client, library = make_flow_client(tmp_path, adapter, FakeFFmpeg())
    with client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        assert client.post("/api/flows/0123456789abcdef/retry", headers=ORIGIN).status_code == 404
        started = start_flow(client, video["id"], "en")
        flow_id = _flow_id(library, started.json()["flow"]["id"])
        wait_for_flow(client, flow_id)
        assert client.post(f"/api/flows/{flow_id}/cancel", headers=ORIGIN).status_code == 409
        assert client.post(f"/api/flows/{flow_id}/retry", headers=ORIGIN).status_code == 409


def test_restart_marks_the_flow_interrupted_and_lets_the_ui_retry_it(tmp_path: Path) -> None:
    """A restarted service must label its unfinished flow instead of forgetting it."""
    adapter = BlockingAdapter()
    client, library = make_flow_client(tmp_path, adapter, FakeFFmpeg())
    with client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        started = start_flow(client, video["id"], "en")
        flow_id = _flow_id(library, started.json()["flow"]["id"])
        assert adapter.entered.wait(5)

        # What `Library.initialize()` does on the next service start, while the worker runs.
        library.interrupt_running()
        adapter.release.set()
        assert library.get_flow(flow_id)["status"] == "interrupted"
        listed = confirm(client, video["id"], target_language="en").json()
        assert listed["latest_flow_id"] == flow_id
        assert listed["busy"] is False
        assert client.get(f"/api/flows/{flow_id}").json()["flow"]["status"] == "interrupted"

        resumed = client.post(f"/api/flows/{flow_id}/retry", headers=ORIGIN)
        assert resumed.status_code == 200, resumed.text
        finished = wait_for_flow(client, flow_id)
        assert {stage["status"] for stage in finished["stages"]} == {"completed"}
