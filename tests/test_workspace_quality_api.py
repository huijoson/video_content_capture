"""Resolution-only quality: the menu, its defaults and the resolved source version (offline)."""

import time
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from tests.test_workspace_s3_api import ORIGIN
from video_content_capture.workspace.app import create_app
from video_content_capture.workspace.config import load_settings
from video_content_capture.workspace.quality import (
    available_resolutions,
    default_resolution,
    resolve_source,
)
from video_content_capture.workspace.youtube import DownloadedMedia, SourceMetadata, parse_metadata

URL = "https://youtu.be/abcdefghijk"

# Extractor order is worst to best; later entries win quality ties.
FORMATS: list[dict[str, object]] = [
    {"format_id": "18", "height": 360, "fps": 30, "vcodec": "avc1.42001E", "acodec": "mp4a.40.2"},
    {"format_id": "a-dub", "vcodec": "none", "acodec": "mp4a.40.2", "language": "en",
     "format_note": "English (dubbed)", "language_preference": 10},
    {"format_id": "a-aac", "vcodec": "none", "acodec": "mp4a.40.2", "language": "ja",
     "format_note": "Japanese (original), medium", "language_preference": 10},
    {"format_id": "a-opus", "vcodec": "none", "acodec": "opus", "language": "ja",
     "format_note": "Japanese (original), medium", "language_preference": 10},
    {"format_id": "v720-avc", "height": 720, "fps": 30, "vcodec": "avc1.4d401f"},
    {"format_id": "v720-vp9", "height": 720, "fps": 60, "vcodec": "vp9"},
    {"format_id": "v1080-avc30", "height": 1080, "fps": 30, "vcodec": "avc1.640028"},
    {"format_id": "v1080-avc60", "height": 1080, "fps": 60, "vcodec": "avc1.64002a"},
    {"format_id": "v1080-vp9", "height": 1080, "fps": 60, "vcodec": "vp9"},
    {"format_id": "v1080-av1", "height": 1080, "fps": 60, "vcodec": "av01.0.09M.08"},
    {"format_id": "v2160", "height": 2160, "fps": 60, "vcodec": "vp9"},
]  # fmt: skip


def source(formats: list[dict[str, object]] = FORMATS) -> SourceMetadata:
    info = {"id": "abcdefghijk", "title": "畫質測試", "duration": 10, "language": "ja"}
    return parse_metadata({**info, "formats": formats}, URL)


class QualityAdapter:
    def __init__(self, metadata: SourceMetadata | None = None) -> None:
        self.source = metadata or source()
        self.downloads: list[tuple[str, str]] = []

    def query(self, url: str, cancel: Event | None = None) -> SourceMetadata:
        return self.source

    def download(self, source, format_id, audio_id, directory, cancel, progress, stages=None):
        self.downloads.append((format_id, audio_id))
        path = directory / "media.mp4"
        path.write_bytes(b"0123456789")
        return DownloadedMedia(
            path=path, container="mp4", video_codec="h264", audio_codec="aac", browser_playable=True
        )


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "VCC_LIBRARY_DIR"):
        monkeypatch.delenv(name, raising=False)


def client_for(tmp_path: Path, adapter: QualityAdapter) -> TestClient:
    return TestClient(
        create_app(load_settings(tmp_path), adapter), base_url="http://127.0.0.1:8765"
    )


def query(client: TestClient) -> dict:
    response = client.post("/api/query", json={"url": URL}, headers=ORIGIN)
    assert response.status_code == 200
    return response.json()


def wait_job(client: TestClient, job_id: str) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if job["status"] not in {"queued", "running"}:
            return job
        time.sleep(0.01)
    pytest.fail("job did not finish within 5 seconds")


def by_height(metadata: dict) -> dict[int, dict]:
    return {entry["height"]: entry for entry in metadata["resolutions"]}


def test_query_lists_distinct_actual_resolutions_with_default(tmp_path: Path) -> None:
    with client_for(tmp_path, QualityAdapter()) as client:
        metadata = query(client)["metadata"]
    # Duplicated heights collapse; nothing upscaled or invented (no 480p, no 1440p).
    assert [entry["height"] for entry in metadata["resolutions"]] == [2160, 1080, 720, 360]
    assert metadata["default_resolution"] == 1080
    assert metadata["above_1080p"] is False
    # Read-only audio: the original-language default track, never the dub.
    assert metadata["audio"]["id"] == "a-opus"
    assert metadata["audio"]["language"] == "ja"
    assert metadata["audio"]["original"] is True


def test_all_above_1080p_preselects_lowest_and_says_so(tmp_path: Path) -> None:
    formats = [
        {"format_id": "a", "vcodec": "none", "acodec": "opus"},
        {"format_id": "v2160", "height": 2160, "fps": 30, "vcodec": "vp9"},
        {"format_id": "v1440", "height": 1440, "fps": 30, "vcodec": "vp9"},
    ]
    with client_for(tmp_path, QualityAdapter(source(formats))) as client:
        metadata = query(client)["metadata"]
    assert [entry["height"] for entry in metadata["resolutions"]] == [2160, 1440]
    assert metadata["default_resolution"] == 1440
    assert metadata["above_1080p"] is True


def test_burned_resolves_highest_fps_best_quality_any_codec(tmp_path: Path) -> None:
    with client_for(tmp_path, QualityAdapter()) as client:
        options = by_height(query(client)["metadata"])
    burned = options[1080]["burned"]
    assert (burned["format_id"], burned["fps"], burned["video_codec"]) == ("v1080-av1", 60, "av1")
    assert burned["audio_id"] == "a-opus"
    assert burned["output_container"] == "mp4"
    assert options[720]["burned"]["format_id"] == "v720-vp9"


def test_tracks_prefers_h264_aac_mp4_then_falls_back_to_best_mkv(tmp_path: Path) -> None:
    with client_for(tmp_path, QualityAdapter()) as client:
        options = by_height(query(client)["metadata"])
    tracks = options[1080]["tracks"]
    assert (tracks["format_id"], tracks["fps"]) == ("v1080-avc60", 60)
    assert tracks["audio_id"] == "a-aac"
    assert (tracks["container"], tracks["output_container"]) == ("mp4", "mp4")
    # H.264 wins over a higher-FPS VP9 so the result stays MP4.
    assert options[720]["tracks"]["format_id"] == "v720-avc"
    # No H.264 at this height: best FPS/quality with the default audio, shown as MKV.
    fallback = options[2160]["tracks"]
    assert (fallback["format_id"], fallback["audio_id"]) == ("v2160", "a-opus")
    assert (fallback["container"], fallback["output_container"]) == ("mkv", "mkv")


def test_tracks_without_aac_audio_uses_best_video_and_mkv() -> None:
    formats = [
        {"format_id": "a", "vcodec": "none", "acodec": "opus"},
        {"format_id": "avc", "height": 720, "fps": 30, "vcodec": "avc1.4d401f"},
        {"format_id": "vp9", "height": 720, "fps": 60, "vcodec": "vp9"},
    ]
    resolved = resolve_source(source(formats), 720, "tracks")
    assert (resolved.format_id, resolved.audio_id, resolved.container) == ("vp9", "a", "mkv")


def test_muxed_format_audio_is_not_a_track_variant() -> None:
    formats = [
        {"format_id": "18", "height": 360, "fps": 30, "vcodec": "avc1.42001E", "acodec": "mp4a"},
        {"format_id": "a", "vcodec": "none", "acodec": "opus"},
        {"format_id": "v", "height": 720, "fps": 30, "vcodec": "avc1.4d401f"},
    ]
    resolved = resolve_source(source(formats), 720, "tracks")
    assert (resolved.format_id, resolved.audio_id, resolved.container) == ("v", "a", "mkv")


def test_resolver_rejects_absent_resolution_and_unknown_form() -> None:
    metadata = source()
    assert available_resolutions(metadata) == [2160, 1080, 720, 360]
    assert default_resolution(metadata) == 1080
    with pytest.raises(ValueError):
        resolve_source(metadata, 480, "tracks")
    with pytest.raises(ValueError):
        resolve_source(metadata, 1080, "overlay")  # type: ignore[arg-type]


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"height": 1080}, ("v1080-avc60", "a-aac", "tracks")),
        ({"height": 1080, "subtitle_form": "tracks"}, ("v1080-avc60", "a-aac", "tracks")),
        ({"height": 1080, "subtitle_form": "burned"}, ("v1080-av1", "a-opus", "burned")),
    ],
)
def test_download_job_resolves_source_from_resolution(
    tmp_path: Path, body: dict, expected: tuple[str, str, str]
) -> None:
    adapter = QualityAdapter()
    with client_for(tmp_path, adapter) as client:
        video_id = query(client)["id"]
        response = client.post(f"/api/videos/{video_id}/jobs", json=body, headers=ORIGIN)
        assert response.status_code == 200
        job = response.json()
        assert (job["format_id"], job["audio_id"], job["resolved"]["subtitle_form"]) == expected
        assert wait_job(client, job["id"])["status"] == "completed"
        assert adapter.downloads == [expected[:2]]
        # Saved assets carry their resolution for the library re-export menu.
        assets = client.get(f"/api/videos/{video_id}").json()["assets"]
        assert [asset["height"] for asset in assets] == [1080]


@pytest.mark.parametrize(
    "body",
    [{"height": 480}, {"height": 1080, "subtitle_form": "overlay"}, {}, {"height": 0}],
)
def test_download_job_rejects_unavailable_resolution(tmp_path: Path, body: dict) -> None:
    adapter = QualityAdapter()
    with client_for(tmp_path, adapter) as client:
        video_id = query(client)["id"]
        response = client.post(f"/api/videos/{video_id}/jobs", json=body, headers=ORIGIN)
        assert response.status_code in {400, 422}
        assert client.get("/api/jobs").json() == []
    assert adapter.downloads == []
