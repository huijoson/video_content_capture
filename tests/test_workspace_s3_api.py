"""S3 public HTTP contracts, with no external provider calls."""

import json
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tests.test_workspace_s2_api import FakeAdapter
from video_content_capture.workspace.app import create_app
from video_content_capture.workspace.config import load_settings

ORIGIN = {"Origin": "http://127.0.0.1:8765"}
SRT = b"1\n00:00:01,000 --> 00:00:02,000\nHello <script>alert(1)</script>world\n"


def wait_for_completed(client: TestClient, job_id: str) -> None:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        progress = client.get(f"/api/jobs/{job_id}").json()
        if progress["status"] not in {"queued", "running"}:
            assert progress["status"] == "completed", progress
            return
    pytest.fail("Background job did not publish within 5 seconds")


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "VCC_LIBRARY_DIR"):
        monkeypatch.delenv(name, raising=False)


def test_import_independent_selections_and_safe_track(tmp_path: Path) -> None:
    class CountingAdapter(FakeAdapter):
        def __init__(self):
            super().__init__()
            self.queries = 0

        def query(self, url, cancel=None):
            self.queries += 1
            return super().query(url, cancel)

    adapter = CountingAdapter()
    with TestClient(
        create_app(load_settings(tmp_path), adapter), base_url="http://127.0.0.1:8765"
    ) as client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        base = f"/api/videos/{video['id']}"
        response = client.post(
            base + "/subtitles/import?language=en&name=Correction&format=srt",
            content=SRT,
            headers={**ORIGIN, "Content-Type": "application/octet-stream"},
        )
        assert response.status_code == 200
        version = response.json()["version"]
        assert version["source_type"] == "import"
        again = client.post(
            base + "/subtitles/import?language=en&name=Correction&format=srt",
            content=SRT,
            headers=ORIGIN,
        ).json()["version"]
        assert again["id"] != version["id"]
        assert len(client.get(base + "/subtitles").json()) == 2
        client.patch(base + "/position", json={"position": 4}, headers=ORIGIN)
        for selection in ("playback", "translation-source", "export-selection"):
            assert (
                client.post(
                    base + "/subtitles/" + selection,
                    json={"version_id": version["id"]},
                    headers=ORIGIN,
                ).status_code
                == 200
            )
        client.post(base + "/subtitles/playback", json={"version_id": None}, headers=ORIGIN)
        opened = client.get(base).json()
        assert opened["position"] == 4
        assert opened["playback_version_id"] is None
        assert opened["translation_source_version_id"] == version["id"]
        assert opened["export_version_id"] == version["id"]
        assert client.get("/api/jobs").json() == []
        assert adapter.downloads == 0
        assert adapter.queries == 1  # Only initial query; selections call no provider.
        track = client.get(f"/api/subtitles/{version['id']}/track.vtt")
        assert track.status_code == 200
        assert track.text.startswith("WEBVTT")
        assert "<script>" not in track.text
        assert client.get(f"/api/subtitles/{version['id']}/download.srt").status_code == 200
        assert client.get("/api/subtitles/not-an-id/track.vtt").status_code == 404
        assert (
            client.post(
                base + "/translations", json={"language": "zh-TW"}, headers=ORIGIN
            ).status_code
            == 409
        )
        assert client.get("/api/jobs").json() == []
        assert client.post(base + "/subtitles/import", content=SRT).status_code == 403


def test_queue_dispatches_non_media_job_through_same_gate(tmp_path: Path) -> None:
    from threading import Event

    from video_content_capture.workspace.jobs import MediaQueue
    from video_content_capture.workspace.storage import Library

    library = Library(tmp_path / "library")
    library.initialize()
    adapter = FakeAdapter()
    source = adapter.source
    video = library.import_video(
        source.youtube_id,
        source.title,
        source.duration,
        source.source_url,
        source.model_dump_json(),
    )
    job = library.create_snapshot_job(str(video["id"]), "translation", {"language": "ja"})
    called = []

    def run(record, attempt, cancel):
        called.append((record["kind"], json.loads(record["snapshot"])))
        library.publish_attempt(str(record["id"]), attempt, lambda db: None)

    queue = MediaQueue(library, adapter, handlers={"translation": run})
    queue.process(str(job["id"]), Event())
    assert called == [("translation", {"language": "ja"})]
    assert library.get_job(str(job["id"]))["status"] == "completed"
    assert adapter.downloads == 0


def test_api_translation_uses_selected_import_without_platform_calls(tmp_path: Path) -> None:
    from dataclasses import replace
    from threading import Event

    from pydantic import SecretStr

    from video_content_capture.workspace.translation import TranslationResponse

    adapter = FakeAdapter()
    entered, release = Event(), Event()
    seen = []

    class FakeTranslation:
        def translate(self, key, model, language, cues):
            seen.append([(cue.id, cue.text, cue.start, cue.end) for cue in cues])
            entered.set()
            assert release.wait(5)
            return TranslationResponse(
                json.dumps({"cues": [{"id": cue.id, "text": "翻譯"} for cue in cues]}), "STOP"
            )

    settings = replace(load_settings(tmp_path), gemini_api_key=SecretStr("fake-api-sentinel"))
    with TestClient(
        create_app(settings, adapter, translation_adapter=FakeTranslation()),
        base_url="http://127.0.0.1:8765",
    ) as client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        base = f"/api/videos/{video['id']}"
        source = client.post(
            base + "/subtitles/import?language=en&name=Correction&format=srt",
            content=SRT,
            headers=ORIGIN,
        ).json()["version"]
        client.post(
            base + "/subtitles/translation-source",
            json={"version_id": source["id"]},
            headers=ORIGIN,
        )
        job = client.post(base + "/translations", json={"language": "zh-TW"}, headers=ORIGIN)
        assert job.status_code == 200
        assert entered.wait(5)
        assert seen == [[("c000001", "Hello world", 1, 2)]]
        assert adapter.downloads == 0
        release.set()
        wait_for_completed(client, job.json()["id"])
    with TestClient(create_app(settings, adapter), base_url="http://127.0.0.1:8765") as client:
        versions = client.get(base + "/subtitles").json()
        target = next(version for version in versions if version["source_type"] == "translation")
        assert target["complete"] == 1
        assert target["parent_id"] == source["id"]
        assert "fake-api-sentinel" not in client.get(base).text
    assert b"fake-api-sentinel" not in (settings.library_dir / "library.sqlite3").read_bytes()


def test_api_export_confirms_snapshot_and_controlled_download(tmp_path: Path) -> None:
    from dataclasses import replace

    from tests.test_workspace_exports import setup_service

    library, video_id, original, target, exporter, _ = setup_service(tmp_path)
    adapter = FakeAdapter()
    library.refresh_metadata(video_id, adapter.source.model_dump_json())
    settings = replace(load_settings(tmp_path), library_dir=library.root)
    with TestClient(
        create_app(settings, adapter, media_exporter=exporter),
        base_url="http://127.0.0.1:8765",
    ) as client:
        base = f"/api/videos/{video_id}"
        response = client.post(
            base + "/exports/preview",
            json={
                "asset_id": "asset",
                "target_version_ids": [target["id"]],
                "include_original": True,
                "original_version_id": original["id"],
            },
            headers=ORIGIN,
        )
        assert response.status_code == 200
        snapshot = response.json()
        assert [track["language"] for track in snapshot["tracks"]] == ["zh-TW", "en"]
        job = client.post(base + "/exports", json={"snapshot": snapshot}, headers=ORIGIN)
        assert job.status_code == 200
        wait_for_completed(client, job.json()["id"])
    with TestClient(create_app(settings, adapter), base_url="http://127.0.0.1:8765") as client:
        artifacts = client.get(base).json()["exports"]
        assert len(artifacts) == 1
        assert "path" not in artifacts[0]
        assert json.loads(artifacts[0]["summary"]) == snapshot
        response = client.get(f"/api/exports/{artifacts[0]['id']}/download")
        assert response.status_code == 200
        assert response.content == b"verified export"
        assert client.get("/api/exports/not-an-id/download").status_code == 404
        assert (
            client.post(
                base + "/exports/preview",
                json={"asset_id": "asset", "target_version_ids": []},
                headers=ORIGIN,
            ).status_code
            == 422
        )


def test_import_clipping_diagnostics_and_bounded_metadata(tmp_path: Path) -> None:
    with TestClient(
        create_app(load_settings(tmp_path), FakeAdapter()), base_url="http://127.0.0.1:8765"
    ) as client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=ORIGIN
        ).json()
        base = f"/api/videos/{video['id']}/subtitles/import"
        clip = b"1\n00:00:09,000 --> 00:00:10,500\nTail\n"
        response = client.post(
            base + "?language=en&name=Tail&format=srt", content=clip, headers=ORIGIN
        )
        assert response.status_code == 200
        assert "cue 1" in response.json()["warnings"][0]
        bad = client.post(
            base + "?language=en&name=Bad&format=srt",
            content=b"1\n00:00:09,000 --> 00:00:12,000\nTail\n",
            headers=ORIGIN,
        )
        assert bad.status_code == 400
        assert "cue 1" in bad.json()["detail"]
        assert (
            client.post(
                base,
                params={"language": "en", "name": "x" * 201, "format": "srt"},
                content=SRT,
                headers=ORIGIN,
            ).status_code
            == 422
        )
        assert len(client.get(f"/api/videos/{video['id']}/subtitles").json()) == 1
