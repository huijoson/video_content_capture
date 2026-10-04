"""S5 HTTP contracts: compatible preview, manual cleanup and re-import, all offline."""

import time
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient
from pydantic import SecretStr

from tests.test_workspace_s2_api import FakeAdapter
from tests.test_workspace_s3_api import ORIGIN
from tests.test_workspace_s4_api import SECRET, FakeQA, import_source, query_video
from video_content_capture.workspace.app import create_app
from video_content_capture.workspace.config import WorkspaceSettings
from video_content_capture.workspace.youtube import DownloadedMedia, SourceError


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "VCC_LIBRARY_DIR"):
        monkeypatch.delenv(name, raising=False)


class UnplayableAdapter(FakeAdapter):
    def __init__(self, playable: bool = False) -> None:
        super().__init__()
        self.playable = playable

    def download(self, source, format_id, audio_id, directory, cancel, progress, stages=None):
        container = "mp4" if self.playable else "mkv"
        path = directory / f"media.{container}"
        path.write_bytes(b"source-media")
        return DownloadedMedia(
            path=path,
            container=container,
            video_codec="h264" if self.playable else "vp9",
            audio_codec="aac" if self.playable else "opus",
            browser_playable=self.playable,
        )


class BlockingEncoder:
    def __init__(self) -> None:
        self.entered = Event()
        self.release = Event()
        self.release.set()
        self.cancelled = Event()
        self.fail = False

    def encode(self, source: Path, directory: Path, cancel: Event) -> Path:
        self.entered.set()
        while not self.release.is_set():
            if cancel.is_set():
                self.cancelled.set()
                raise SourceError("cancelled", "工作已取消")
            time.sleep(0.01)
        if self.fail:
            raise SourceError("preview_failed", "相容預覽製作失敗")
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / "out.mp4"
        output.write_bytes(b"preview-media")
        return output


def wait_job(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    pytest.fail("job did not finish within 5 seconds")


def make_client(tmp_path: Path, adapter=None, encoder=None, qa=None, key=False) -> TestClient:
    settings = WorkspaceSettings(
        tmp_path,
        tmp_path / "library",
        gemini_api_key=SecretStr(SECRET) if key else None,
    )
    app = create_app(
        settings, adapter or UnplayableAdapter(), qa_adapter=qa, preview_encoder=encoder
    )
    return TestClient(app, base_url="http://127.0.0.1:8765")


def download(client: TestClient, video_id: str) -> dict:
    job = client.post(
        f"/api/videos/{video_id}/jobs", json={"format_id": "v", "audio_id": "a"}, headers=ORIGIN
    ).json()
    assert wait_job(client, job["id"])["status"] == "completed"
    return client.get(f"/api/videos/{video_id}").json()["assets"][0]


def test_playable_source_rejects_preview_and_plays_directly(tmp_path: Path) -> None:
    encoder = BlockingEncoder()
    with make_client(tmp_path, UnplayableAdapter(playable=True), encoder) as client:
        video_id = query_video(client)
        asset = download(client, video_id)
        assert asset["browser_playable"] == 1
        response = client.post(
            f"/api/videos/{video_id}/previews", json={"asset_id": asset["id"]}, headers=ORIGIN
        )
        assert response.status_code == 409
        assert not encoder.entered.is_set()
        assert client.get(f"/api/videos/{video_id}").json()["previews"] == []


def test_unplayable_preview_flow_media_route_and_clear(tmp_path: Path) -> None:
    encoder = BlockingEncoder()
    with make_client(tmp_path, encoder=encoder) as client:
        video_id = query_video(client)
        asset = download(client, video_id)
        source_id = import_source(client, video_id)
        job = client.post(
            f"/api/videos/{video_id}/previews", json={"asset_id": asset["id"]}, headers=ORIGIN
        ).json()
        assert job["kind"] == "preview"
        assert wait_job(client, job["id"])["status"] == "completed"
        opened = client.get(f"/api/videos/{video_id}").json()
        assert len(opened["previews"]) == 1
        preview = opened["previews"][0]
        assert preview["asset_id"] == asset["id"]
        assert "path" not in preview and "checksum" not in preview
        media = client.get(f"/api/previews/{preview['id']}/media")
        assert media.status_code == 200
        assert media.content == b"preview-media"
        assert media.headers["content-type"] == "video/mp4"
        assert client.get(f"/api/assets/{asset['id']}/media").content == b"source-media"
        # Existing preview is not rebuilt silently.
        again = client.post(
            f"/api/videos/{video_id}/previews", json={"asset_id": asset["id"]}, headers=ORIGIN
        )
        assert again.status_code == 409
        cleared = client.delete(f"/api/videos/{video_id}/previews", headers=ORIGIN)
        assert cleared.status_code == 200 and cleared.json()["removed"] == 1
        assert client.get(f"/api/previews/{preview['id']}/media").status_code == 404
        reopened = client.get(f"/api/videos/{video_id}").json()
        assert reopened["previews"] == []
        assert len(reopened["assets"]) == 1
        assert [v["id"] for v in reopened["subtitles"]] == [source_id]
        assert client.get(f"/api/assets/{asset['id']}/media").content == b"source-media"
        # After clearing, rebuilding is an explicit user action again.
        rebuilt = client.post(
            f"/api/videos/{video_id}/previews", json={"asset_id": asset["id"]}, headers=ORIGIN
        )
        assert rebuilt.status_code == 200
        assert wait_job(client, rebuilt.json()["id"])["status"] == "completed"


def test_preview_failure_keeps_subtitle_downloads(tmp_path: Path) -> None:
    encoder = BlockingEncoder()
    encoder.fail = True
    with make_client(tmp_path, encoder=encoder) as client:
        video_id = query_video(client)
        asset = download(client, video_id)
        source_id = import_source(client, video_id)
        job = client.post(
            f"/api/videos/{video_id}/previews", json={"asset_id": asset["id"]}, headers=ORIGIN
        ).json()
        failed = wait_job(client, job["id"])
        assert failed["status"] == "failed" and failed["error_code"] == "preview_failed"
        assert client.get(f"/api/subtitles/{source_id}/download.srt").status_code == 200
        assert client.get(f"/api/subtitles/{source_id}/track.vtt").status_code == 200
        assert client.post(f"/api/jobs/{job['id']}/retry", headers=ORIGIN).status_code == 200


def test_cleanup_routes_reject_traversal_and_cross_site(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        video_id = query_video(client)
        for path in (
            "/api/previews/..%2F..%2Flibrary.sqlite3/media",
            "/api/videos/..%2F..%2Fetc/deletion",
        ):
            assert client.get(path).status_code == 404
        assert client.post("/api/deletions/..%2F..%2F/retry", headers=ORIGIN).status_code == 404
        assert client.delete("/api/videos/not-a-video/previews", headers=ORIGIN).status_code == 404
        cross = client.post(
            f"/api/videos/{video_id}/delete",
            json={"confirmation": "x"},
            headers={"Origin": "http://evil.example"},
        )
        assert cross.status_code == 403
        assert client.get(f"/api/videos/{video_id}").status_code == 200


def test_delete_requires_scope_confirmation(tmp_path: Path) -> None:
    with make_client(tmp_path) as client:
        video_id = query_video(client)
        download(client, video_id)
        import_source(client, video_id)
        scope = client.get(f"/api/videos/{video_id}/deletion").json()
        assert scope["assets"] == 1 and scope["subtitle_versions"] == 1
        assert scope["exports"] == 0 and scope["previews"] == 0
        assert scope["size_bytes"] >= len(b"source-media")
        missing = client.post(f"/api/videos/{video_id}/delete", json={}, headers=ORIGIN)
        assert missing.status_code in {400, 422}
        wrong = client.post(
            f"/api/videos/{video_id}/delete", json={"confirmation": "0" * 64}, headers=ORIGIN
        )
        assert wrong.status_code == 409
        assert client.get(f"/api/videos/{video_id}").status_code == 200
        deleted = client.post(
            f"/api/videos/{video_id}/delete",
            json={"confirmation": scope["confirmation"]},
            headers=ORIGIN,
        )
        assert deleted.status_code == 200 and deleted.json() == {"deleted": True}
        assert client.get(f"/api/videos/{video_id}").status_code == 404
        assert client.get("/api/videos").json() == []
        assert client.get("/api/deletions").json() == []
        assert not (tmp_path / "library" / "videos" / video_id).exists()


def test_delete_cancels_running_preview_and_terminates_its_work(tmp_path: Path) -> None:
    encoder = BlockingEncoder()
    with make_client(tmp_path, encoder=encoder) as client:
        video_id = query_video(client)
        asset = download(client, video_id)
        encoder.release.clear()
        job = client.post(
            f"/api/videos/{video_id}/previews", json={"asset_id": asset["id"]}, headers=ORIGIN
        ).json()
        assert encoder.entered.wait(5)
        scope = client.get(f"/api/videos/{video_id}/deletion").json()
        deleted = client.post(
            f"/api/videos/{video_id}/delete",
            json={"confirmation": scope["confirmation"]},
            headers=ORIGIN,
        )
        assert deleted.status_code == 200
        assert encoder.cancelled.is_set()
        assert client.get(f"/api/jobs/{job['id']}").status_code == 404
        assert not (tmp_path / "library" / "videos" / video_id).exists()


def test_stuck_media_job_leaves_retryable_pending_cleanup(tmp_path: Path, monkeypatch) -> None:
    from video_content_capture.workspace import app as app_module

    monkeypatch.setattr(app_module, "DELETE_WAIT_SECONDS", 0.2)
    stuck, release = Event(), Event()

    class StuckEncoder(BlockingEncoder):
        def encode(self, source, directory, cancel):
            stuck.set()
            assert release.wait(5)  # ignores cancellation, like an uninterruptible call
            raise SourceError("cancelled", "工作已取消")

    with make_client(tmp_path, encoder=StuckEncoder()) as client:
        video_id = query_video(client)
        asset = download(client, video_id)
        client.post(
            f"/api/videos/{video_id}/previews", json={"asset_id": asset["id"]}, headers=ORIGIN
        )
        assert stuck.wait(5)
        scope = client.get(f"/api/videos/{video_id}/deletion").json()
        response = client.post(
            f"/api/videos/{video_id}/delete",
            json={"confirmation": scope["confirmation"]},
            headers=ORIGIN,
        )
        assert response.status_code == 409
        assert client.get("/api/videos").json() == []
        pending = client.get("/api/deletions").json()
        assert [item["id"] for item in pending] == [video_id]
        assert client.get(f"/api/videos/{video_id}").status_code == 404
        release.set()
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            retried = client.post(f"/api/deletions/{video_id}/retry", headers=ORIGIN)
            if retried.status_code == 200:
                break
            time.sleep(0.05)
        assert retried.json() == {"deleted": True}
        assert client.get("/api/deletions").json() == []
        assert not (tmp_path / "library" / "videos" / video_id).exists()


def test_delete_reimport_then_old_reply_arrives_is_not_written(tmp_path: Path) -> None:
    fake = FakeQA()
    with make_client(tmp_path, FakeAdapter(), qa=fake, key=True) as client:
        old_video = query_video(client)
        old_source = import_source(client, old_video)
        conversation = client.post(
            f"/api/videos/{old_video}/conversations",
            json={"version_id": old_source},
            headers=ORIGIN,
        ).json()
        fake.release.clear()
        asked = client.post(
            f"/api/conversations/{conversation['id']}/messages",
            json={"question": "old question"},
            headers=ORIGIN,
        ).json()
        assert fake.entered.wait(5)
        scope = client.get(f"/api/videos/{old_video}/deletion").json()
        assert scope["conversations"] == 1
        started = time.monotonic()
        deleted = client.post(
            f"/api/videos/{old_video}/delete",
            json={"confirmation": scope["confirmation"]},
            headers=ORIGIN,
        )
        # Deleting never waits for an uninterruptible cloud reply; the gate discards it.
        assert deleted.status_code == 200 and time.monotonic() - started < 4
        new_video = query_video(client)
        assert new_video != old_video
        new_source = import_source(client, new_video)
        new_conversation = client.post(
            f"/api/videos/{new_video}/conversations",
            json={"version_id": new_source},
            headers=ORIGIN,
        ).json()
        fake.release.set()
        follow = client.post(
            f"/api/conversations/{new_conversation['id']}/messages",
            json={"question": "new question"},
            headers=ORIGIN,
        ).json()
        # QA is serial: the new answer completes only after the old reply was handled.
        assert wait_job(client, follow["request_id"])["status"] == "completed"
        assert [call[0] for call in fake.calls] == ["old question", "new question"]
        messages = client.get(f"/api/conversations/{new_conversation['id']}").json()["messages"]
        assert [m["question"] for m in messages] == ["new question"]
        assert client.get(f"/api/conversations/{conversation['id']}").status_code == 404
        assert client.get(f"/api/jobs/{asked['request_id']}").status_code == 404
