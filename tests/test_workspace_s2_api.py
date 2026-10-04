from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from video_content_capture.workspace.app import create_app
from video_content_capture.workspace.config import load_settings
from video_content_capture.workspace.jobs import MediaQueue
from video_content_capture.workspace.storage import Library
from video_content_capture.workspace.youtube import DownloadedMedia, SourceMetadata, parse_metadata


@pytest.fixture(autouse=True)
def isolated_workspace_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "VCC_LIBRARY_DIR",
        "VCC_HOST",
        "VCC_PORT",
        "VCC_GEMINI_TRANSLATION_MODEL",
        "VCC_GEMINI_QA_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_invalid_query_and_controlled_media(tmp_path: Path) -> None:
    with TestClient(
        create_app(load_settings(tmp_path)), base_url="http://127.0.0.1:8765"
    ) as client:
        response = client.post(
            "/api/query", json={"url": "/etc/passwd"}, headers={"Origin": "http://127.0.0.1:8765"}
        )
        assert response.status_code == 400
        assert client.get("/api/assets/not-an-id/media").status_code == 404


class FakeAdapter:
    def __init__(self) -> None:
        self.source = parse_metadata(
            {
                "id": "abcdefghijk",
                "title": "離線影片",
                "duration": 10,
                "formats": [
                    {"format_id": "v", "height": 720, "vcodec": "avc1", "acodec": "none"},
                    {"format_id": "a", "vcodec": "none", "acodec": "mp4a"},
                ],
            },
            "https://youtu.be/abcdefghijk",
        )
        self.downloads = 0
        self.callback = lambda: None

    def query(self, url: str, cancel: Event | None = None) -> SourceMetadata:
        return self.source

    def download(self, source, format_id, audio_id, directory, cancel, progress, stages=None):
        self.downloads += 1
        self.callback()
        path = directory / "media.mp4"
        path.write_bytes(b"0123456789")
        return DownloadedMedia(
            path=path, container="mp4", video_codec="h264", audio_codec="aac", browser_playable=True
        )


def setup_queue(tmp_path):
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
    job = library.create_job(str(video["id"]), "v", "a")
    return library, adapter, MediaQueue(library, adapter), job


def test_media_publication_reuse_and_range(tmp_path: Path) -> None:
    library, adapter, queue, job = setup_queue(tmp_path)
    queue.process(str(job["id"]), Event())
    assert library.get_job(str(job["id"]))["status"] == "completed"
    assets = library.assets(str(job["video_id"]))
    assert len(assets) == 1
    second = library.create_job(str(job["video_id"]), "v", "a")
    queue.process(str(second["id"]), Event())
    assert adapter.downloads == 1
    settings = replace(load_settings(tmp_path), library_dir=library.root)
    with TestClient(create_app(settings, adapter), base_url="http://127.0.0.1:8765") as client:
        response = client.get(
            f"/api/assets/{assets[0]['id']}/media", headers={"Range": "bytes=2-5"}
        )
        assert response.status_code == 206
        assert response.content == b"2345"
        assert response.headers["content-range"] == "bytes 2-5/10"
        assert client.get("/api/videos/no-such-id").status_code == 404
        assert (
            client.post(
                "/api/jobs/missing/cancel", headers={"Origin": "http://127.0.0.1:8765"}
            ).status_code
            == 404
        )


@pytest.mark.parametrize("action", ["cancel_retry", "delete"])
def test_late_media_cannot_publish(tmp_path: Path, action: str) -> None:
    library, adapter, queue, job = setup_queue(tmp_path)

    def late():
        if action == "delete":
            library.mark_deleting(str(job["video_id"]))
        else:
            library.cancel_job(str(job["id"]))
            library.retry_job(str(job["id"]))

    adapter.callback = late
    queue.process(str(job["id"]), Event())
    assert library.assets(str(job["video_id"])) == []
    assert not list((library.video_dir(str(job["video_id"])) / "source").iterdir())


def test_space_and_missing_format_fail_without_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    library, adapter, queue, job = setup_queue(tmp_path)
    from collections import namedtuple

    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        "video_content_capture.workspace.jobs.shutil.disk_usage", lambda root: usage(1, 1, 0)
    )
    queue.process(str(job["id"]), Event())
    assert library.get_job(str(job["id"]))["error_code"] == "insufficient_space"
    assert adapter.downloads == 0
    library.retry_job(str(job["id"]))
    adapter.source = adapter.source.model_copy(update={"formats": []})
    queue.process(str(job["id"]), Event())
    assert library.get_job(str(job["id"]))["error_code"] == "format_missing"
    assert adapter.downloads == 0


def test_cancel_immediate_retry_is_not_lost_and_queue_is_serial(tmp_path: Path) -> None:
    library, adapter, queue, job = setup_queue(tmp_path)
    entered, release, finished = Event(), Event(), Event()

    def blocking():
        if adapter.downloads == 1:
            entered.set()
            assert release.wait(5)
        else:
            finished.set()

    adapter.callback = blocking
    queue.start()
    try:
        queue.enqueue(job)
        assert entered.wait(5)
        queue.cancel(str(job["id"]))
        library.retry_job(str(job["id"]))
        queue.enqueue(library.get_job(str(job["id"])))
        release.set()
        assert finished.wait(5)
    finally:
        # Join while the second result settles, without stopping it before publication.
        with queue.condition:
            assert queue.condition.wait_for(
                lambda: not queue.active and not queue.pending, timeout=5
            )
        queue.close()
    assert adapter.downloads == 2
    assert library.get_job(str(job["id"]))["status"] == "completed"
    assert len(library.assets(str(job["video_id"]))) == 1


def test_api_query_dedup_refresh_position_and_background(tmp_path: Path) -> None:
    adapter = FakeAdapter()
    entered, release = Event(), Event()

    def blocking():
        entered.set()
        assert release.wait(5)

    adapter.callback = blocking
    headers = {"Origin": "http://127.0.0.1:8765"}
    with TestClient(
        create_app(load_settings(tmp_path), adapter), base_url="http://127.0.0.1:8765"
    ) as client:
        video = client.post(
            "/api/query", json={"url": "https://youtu.be/abcdefghijk"}, headers=headers
        ).json()
        again = client.post(
            "/api/query",
            json={"url": "https://youtube.com/watch?v=abcdefghijk&list=some"},
            headers=headers,
        ).json()
        assert again["id"] == video["id"]
        adapter.source = adapter.source.model_copy(update={"default_format_id": "v"})
        assert (
            client.post(
                "/api/query",
                json={"url": "https://youtu.be/abcdefghijk", "refresh": True},
                headers=headers,
            ).status_code
            == 200
        )
        path = f"/api/videos/{video['id']}"
        assert (
            client.patch(path + "/position", json={"position": 4}, headers=headers).status_code
            == 200
        )
        assert client.get(path).json()["position"] == 4
        job = client.post(
            path + "/jobs", json={"format_id": "v", "audio_id": "a"}, headers=headers
        ).json()
        assert entered.wait(5)
        assert client.get("/api/jobs").json()[0]["status"] == "running"
        duplicate = client.post(
            path + "/jobs", json={"format_id": "v", "audio_id": "a"}, headers=headers
        ).json()
        assert duplicate["id"] == job["id"]
        release.set()


def test_restart_never_starts_saved_queue(tmp_path: Path) -> None:
    library, adapter, queue, job = setup_queue(tmp_path)
    library.start_job(str(job["id"]))
    settings = replace(load_settings(tmp_path), library_dir=library.root)
    with TestClient(create_app(settings, adapter), base_url="http://127.0.0.1:8765") as client:
        assert client.get("/api/jobs").json()[0]["status"] == "interrupted"
        assert adapter.downloads == 0


def test_stage_checkpoint_reuses_only_verified_matching_file(tmp_path: Path) -> None:
    from video_content_capture.workspace.jobs import StageStore

    library, adapter, queue, job = setup_queue(tmp_path)
    attempt = library.start_job(str(job["id"]))
    stages = StageStore(library, str(job["id"]), attempt, str(job["video_id"]))
    temporary = tmp_path / "stream.mp4"
    temporary.write_bytes(b"verified")
    stages.save("video", "v", temporary)
    loaded = stages.load("video", "v")
    assert loaded is not None and loaded.read_bytes() == b"verified"
    assert stages.load("video", "other") is None
    loaded.write_bytes(b"corrupt")
    assert stages.load("video", "v") is None
    library.cancel_job(str(job["id"]))
    with pytest.raises(Exception, match="失效"):
        stages.save("audio", "a", temporary)
    assert len(library.stage_records(str(job["id"]))) == 1


def test_binary_media_is_byte_exact_with_configured_fake_key(tmp_path: Path) -> None:
    library, adapter, queue, job = setup_queue(tmp_path)
    queue.process(str(job["id"]), Event())
    asset = library.assets(str(job["video_id"]))[0]
    (tmp_path / ".env").write_text("GEMINI_API_KEY=2345\n")
    settings = replace(load_settings(tmp_path), library_dir=library.root)
    with TestClient(create_app(settings, adapter), base_url="http://127.0.0.1:8765") as client:
        response = client.get(f"/api/assets/{asset['id']}/media", headers={"Range": "bytes=2-5"})
        assert response.content == b"2345"
        assert response.headers["content-length"] == "4"


def test_space_estimate_includes_verified_checkpoints(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from collections import namedtuple

    library, adapter, queue, job = setup_queue(tmp_path)
    megabyte = 1024 * 1024
    adapter.source = adapter.source.model_copy(
        update={
            "formats": [adapter.source.formats[0].model_copy(update={"size": 100 * megabyte})],
            "audio_tracks": [
                adapter.source.audio_tracks[0].model_copy(update={"size": 100 * megabyte})
            ],
        }
    )
    usage = namedtuple("usage", "total used free")
    monkeypatch.setattr(
        "video_content_capture.workspace.jobs.shutil.disk_usage",
        lambda root: usage(1000 * megabyte, 400 * megabyte, 600 * megabyte),
    )
    queue.process(str(job["id"]), Event())
    assert library.get_job(str(job["id"]))["error_code"] == "insufficient_space"
    assert adapter.downloads == 0


def test_metadata_is_redacted_before_persistence(tmp_path: Path) -> None:
    secret = "fake-metadata-sentinel"
    (tmp_path / ".env").write_text(f"GEMINI_API_KEY={secret}\n")
    adapter = FakeAdapter()
    adapter.source = adapter.source.model_copy(
        update={
            "title": secret,
            "original_language": secret,
            "audio_tracks": [
                adapter.source.audio_tracks[0].model_copy(update={"language": secret})
            ],
        }
    )
    with TestClient(
        create_app(load_settings(tmp_path), adapter), base_url="http://127.0.0.1:8765"
    ) as client:
        response = client.post(
            "/api/query",
            json={"url": "https://youtu.be/abcdefghijk"},
            headers={"Origin": "http://127.0.0.1:8765"},
        )
        assert response.status_code == 200
        assert secret not in response.text
    assert secret.encode() not in (tmp_path / "outputs/library/library.sqlite3").read_bytes()


class StagingAdapter(FakeAdapter):
    """Checkpoints a stage, then fails with ``self.failure`` or publishes."""

    def __init__(self) -> None:
        super().__init__()
        self.failure: Exception | None = None

    def download(self, source, format_id, audio_id, directory, cancel, progress, stages=None):
        assert stages is not None
        if stages.load("video", format_id) is None:
            stream = directory / "video.part"
            stream.write_bytes(b"staged")
            stages.save("video", format_id, stream)
        progress("merge", None, None)
        if self.failure is not None:
            raise self.failure
        return super().download(source, format_id, audio_id, directory, cancel, progress, stages)


def staging_queue(tmp_path: Path):
    library, _, _, job = setup_queue(tmp_path)
    adapter = StagingAdapter()
    return library, adapter, MediaQueue(library, adapter), job


def stage_files(library: Library, job: dict) -> list[Path]:
    return sorted((library.video_dir(str(job["video_id"])) / "source").glob("stage-*.bin"))


def test_download_failure_logs_one_redacted_line_and_keeps_stages(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from video_content_capture.redaction import clear_secrets, register_secrets

    secret = "download-key-sentinel"
    library, adapter, queue, job = staging_queue(tmp_path)
    adapter.failure = RuntimeError(f"boom key={secret}\nsecond line")
    register_secrets([secret])
    try:
        with caplog.at_level(logging.WARNING, logger="vcc.workspace"):
            queue.process(str(job["id"]), Event())
    finally:
        clear_secrets()
    assert library.get_job(str(job["id"]))["error_code"] == "media_failed"
    records = [r for r in caplog.records if r.name == "vcc.workspace"]
    assert len(records) == 1 and records[0].levelno == logging.WARNING
    line = records[0].getMessage()
    for part in (str(job["id"]), "stage=merge", "code=media_failed", "RuntimeError", "[REDACTED]"):
        assert part in line
    assert secret not in line and "\n" not in line
    # The verified stage survives so a manual retry resumes instead of re-downloading.
    assert len(stage_files(library, job)) == 1
    assert len(library.stage_records(str(job["id"]))) == 1


def test_source_error_failure_is_logged_with_its_code(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    import logging

    from video_content_capture.workspace.youtube import SourceError

    library, adapter, queue, job = staging_queue(tmp_path)
    adapter.failure = SourceError("source_unavailable", "來源不支援／無法取得")
    with caplog.at_level(logging.WARNING, logger="vcc.workspace"):
        queue.process(str(job["id"]), Event())
    records = [r for r in caplog.records if r.name == "vcc.workspace"]
    assert len(records) == 1
    assert "code=source_unavailable" in records[0].getMessage()
    assert len(stage_files(library, job)) == 1


def test_published_download_removes_its_stage_files(tmp_path: Path) -> None:
    library, adapter, queue, job = staging_queue(tmp_path)
    adapter.failure = RuntimeError("transient")
    queue.process(str(job["id"]), Event())
    assert len(stage_files(library, job)) == 1
    adapter.failure = None
    library.retry_job(str(job["id"]))
    queue.process(str(job["id"]), Event())
    assert library.get_job(str(job["id"]))["status"] == "completed"
    assert len(library.assets(str(job["video_id"]))) == 1
    assert stage_files(library, job) == []
    assert library.stage_records(str(job["id"])) == []
