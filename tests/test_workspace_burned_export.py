"""Burned-in subtitle export: public HTTP contract with a fake ffmpeg, plus real ffmpeg."""

import json
import shutil
import sqlite3
import subprocess
import time
from collections import namedtuple
from dataclasses import replace
from pathlib import Path
from threading import Event

import pytest
from fastapi.testclient import TestClient

from tests.test_workspace_s2_api import FakeAdapter
from video_content_capture.workspace import exports
from video_content_capture.workspace.app import create_app
from video_content_capture.workspace.burned import SubtitleLayout, render_ass, wrap_line
from video_content_capture.workspace.config import load_settings
from video_content_capture.workspace.exports import ExportTrack, MediaExporter
from video_content_capture.workspace.storage import Library
from video_content_capture.workspace.subtitles import Cue
from video_content_capture.workspace.youtube import SourceError

ORIGIN = {"Origin": "http://127.0.0.1:8765"}


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in ("GEMINI_API_KEY", "GOOGLE_API_KEY", "VCC_LIBRARY_DIR"):
        monkeypatch.delenv(name, raising=False)


class FakeFFmpeg:
    """Answers ffprobe/ffmpeg like the real tools; records every command it receives."""

    def __init__(self, audio_codec: str = "aac") -> None:
        self.audio_codec = audio_codec
        self.hardware = True
        self.calls: list[list[str]] = []
        self.subtitles: list[str] = []
        self.block = False
        self.entered = Event()
        self.release = Event()
        self.cancelled = Event()

    def __call__(self, args: list[str], cancel: Event) -> str:
        self.calls.append(args)
        if args[0] == "ffprobe":
            path = Path(args[-1])
            burned = path.suffix == ".mp4" and path.parent.name != "source"
            audio = "aac" if burned else self.audio_codec
            return json.dumps(
                {
                    "streams": [
                        {"codec_type": "video", "codec_name": "h264", "width": 640, "height": 360},
                        {"codec_type": "audio", "codec_name": audio, "bit_rate": "128000"},
                    ],
                    "format": {"format_name": "mov,mp4,m4a,3gp,3g2,mj2", "duration": "10.0"},
                }
            )
        if "lavfi" in args:
            if not self.hardware:
                raise SourceError("source_unavailable", "no VideoToolbox")
            return ""
        output = Path(args[-1])
        self.subtitles.append((output.parent / "burned.ass").read_text(encoding="utf-8"))
        report = Path(args[args.index("-progress") + 1])
        report.write_text("out_time_us=5000000\nprogress=continue\n")
        if self.block and "-t" not in args:
            self.entered.set()
            while not self.release.wait(0.01):
                if cancel.is_set():
                    # run_process terminates the ffmpeg process group at this point.
                    self.cancelled.set()
                    raise SourceError("cancelled", "工作已取消")
        output.write_bytes(b"burned export")
        return ""

    def burns(self) -> list[list[str]]:
        return [args for args in self.calls if "-progress" in args and "-t" not in args]


def make_client(tmp_path: Path, runner: FakeFFmpeg):
    from tests.test_workspace_exports import setup_service

    library, video_id, original, target, _, _ = setup_service(tmp_path)
    adapter = FakeAdapter()
    library.refresh_metadata(video_id, adapter.source.model_dump_json())
    settings = replace(load_settings(tmp_path), library_dir=library.root)
    client = TestClient(
        create_app(settings, adapter, media_exporter=MediaExporter(runner)),
        base_url="http://127.0.0.1:8765",
    )
    return client, library, video_id, original, target


def preview(client: TestClient, video_id: str, target: dict, **extra: object):
    return client.post(
        f"/api/videos/{video_id}/exports/preview",
        json={"asset_id": "asset", "target_version_ids": [target["id"]], **extra},
        headers=ORIGIN,
    )


def wait_job(client: TestClient, job_id: str, until=None) -> dict:
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        job = client.get(f"/api/jobs/{job_id}").json()
        if (until or (lambda j: j["status"] not in {"queued", "running"}))(job):
            return job
        time.sleep(0.01)
    pytest.fail("Background export did not reach the expected state within 5 seconds")


def test_burned_export_publishes_new_mp4_artifact_with_subtitle_form(tmp_path: Path) -> None:
    runner = FakeFFmpeg()
    client, library, video_id, _, target = make_client(tmp_path, runner)
    source = library.root / str(library.get_asset("asset")["path"])
    with client:
        response = preview(client, video_id, target, subtitle_form="burned")
        assert response.status_code == 200
        snapshot = response.json()
        assert snapshot["subtitle_form"] == "burned"
        assert snapshot["container"] == "mp4"
        assert [track["version_id"] for track in snapshot["tracks"]] == [target["id"]]
        # The confirmation step already proved VideoToolbox with a short trial encode.
        assert any("-t" in args and "-progress" in args for args in runner.calls)
        artifacts = []
        for _ in range(2):
            job = client.post(
                f"/api/videos/{video_id}/exports", json={"snapshot": snapshot}, headers=ORIGIN
            )
            assert job.status_code == 200
            assert wait_job(client, job.json()["id"])["status"] == "completed"
        artifacts = client.get(f"/api/videos/{video_id}").json()["exports"]
        assert len(artifacts) == 2
        assert {artifact["subtitle_form"] for artifact in artifacts} == {"burned"}
        assert {artifact["container"] for artifact in artifacts} == {"mp4"}
        assert json.loads(artifacts[0]["summary"]) == snapshot
        download = client.get(f"/api/exports/{artifacts[0]['id']}/download")
        assert download.status_code == 200
        assert download.content == b"burned export"
        assert download.headers["content-type"] == "video/mp4"
    paths = {row["path"] for row in library.exports(video_id)}
    assert len(paths) == 2 and all(str(path).endswith(".mp4") for path in paths)
    assert source.read_bytes() == b"source"
    burn = runner.burns()[0]
    assert burn[burn.index("-c:v") + 1] == "h264_videotoolbox"
    assert burn[burn.index("-allow_sw") + 1] == "0"
    assert burn[burn.index("-c:a") + 1] == "copy"
    assert "scale" not in burn[burn.index("-vf") + 1]
    assert not any("libx264" in args for args in runner.calls)
    ass = runner.subtitles[-1]
    # setup_service's cue runs 0.0–0.5 s; the burned subtitle keeps that timing.
    assert "Dialogue: 0,0:00:00.00,0:00:00.50,Default,,0,0,0,,original" in ass
    assert "PlayResX: 640\nPlayResY: 360" in ass
    style = next(line for line in ass.splitlines() if line.startswith("Style:")).split(",")
    assert style[1] == "Heiti TC"  # zh-TW target uses a Traditional Chinese system font
    assert style[3] == "&H00FFFFFF" and style[5] == "&H00000000"  # white text, black edge
    assert style[18] == "2"  # bottom centre
    assert int(style[2]) == round(360 * 0.05)


def test_burned_export_transcodes_non_aac_audio(tmp_path: Path) -> None:
    runner = FakeFFmpeg(audio_codec="opus")
    client, library, video_id, _, target = make_client(tmp_path, runner)
    with client:
        snapshot = preview(client, video_id, target, subtitle_form="burned").json()
        job = client.post(
            f"/api/videos/{video_id}/exports", json={"snapshot": snapshot}, headers=ORIGIN
        ).json()
        assert wait_job(client, job["id"])["status"] == "completed"
    burn = runner.burns()[0]
    assert burn[burn.index("-c:a") + 1] == "aac"


def test_burned_progress_reports_processed_duration_and_cancel_stops_ffmpeg(
    tmp_path: Path,
) -> None:
    runner = FakeFFmpeg()
    client, library, video_id, _, target = make_client(tmp_path, runner)
    with client:
        snapshot = preview(client, video_id, target, subtitle_form="burned").json()
        runner.block = True
        job = client.post(
            f"/api/videos/{video_id}/exports", json={"snapshot": snapshot}, headers=ORIGIN
        ).json()
        assert runner.entered.wait(5)
        # The fake reports 5 s processed of a 10 s source.
        running = wait_job(client, job["id"], lambda j: j["progress"] == 0.5)
        assert running["stage"] == "burning"
        assert running["status"] == "running"
        assert client.post(f"/api/jobs/{job['id']}/cancel", headers=ORIGIN).status_code == 200
        assert runner.cancelled.wait(5)
        assert wait_job(client, job["id"])["status"] == "cancelled"
        assert client.get(f"/api/videos/{video_id}").json()["exports"] == []


def test_hardware_encoder_unavailable_fails_explicitly_without_software_fallback(
    tmp_path: Path,
) -> None:
    runner = FakeFFmpeg()
    client, library, video_id, _, target = make_client(tmp_path, runner)
    with client:
        snapshot = preview(client, video_id, target, subtitle_form="burned").json()
        runner.hardware = False
        refused = preview(client, video_id, target, subtitle_form="burned")
        assert refused.status_code == 409
        assert "VideoToolbox" in refused.json()["detail"]
        # A confirmed job whose hardware disappears fails with an explicit code.
        job = library.create_snapshot_job(video_id, "export", snapshot)
        from video_content_capture.workspace.exports import ExportService

        service = ExportService(library, MediaExporter(runner))
        service.run(job, library.start_job(str(job["id"])), Event())
        failed = client.get(f"/api/jobs/{job['id']}").json()
        assert failed["status"] == "failed"
        assert failed["error_code"] == "hardware_encoder_unavailable"
        assert client.get(f"/api/videos/{video_id}").json()["exports"] == []
    assert not any("libx264" in args for args in runner.calls)
    assert len(runner.burns()) == 0


def test_burned_export_space_precheck_blocks_encode(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    runner = FakeFFmpeg()
    client, library, video_id, _, target = make_client(tmp_path, runner)
    with client:
        snapshot = preview(client, video_id, target, subtitle_form="burned").json()
        usage = namedtuple("Usage", "total used free")
        monkeypatch.setattr(exports.shutil, "disk_usage", lambda root: usage(100, 100, 0))
        job = client.post(
            f"/api/videos/{video_id}/exports", json={"snapshot": snapshot}, headers=ORIGIN
        ).json()
        failed = wait_job(client, job["id"])
        assert failed["error_code"] == "insufficient_space"
    assert runner.burns() == []
    assert len(library.subtitle_cues(str(target["id"]))) == 1


def test_tracks_form_keeps_existing_matrix_and_burned_rejects_extra_tracks(
    tmp_path: Path,
) -> None:
    from tests.test_workspace_exports import setup_service

    library, video_id, original, target, exporter, _ = setup_service(tmp_path)
    adapter = FakeAdapter()
    library.refresh_metadata(video_id, adapter.source.model_dump_json())
    settings = replace(load_settings(tmp_path), library_dir=library.root)
    with TestClient(
        create_app(settings, adapter, media_exporter=exporter), base_url="http://127.0.0.1:8765"
    ) as client:
        both = {"include_original": True, "original_version_id": original["id"]}
        default = preview(client, video_id, target, **both).json()
        explicit = preview(client, video_id, target, subtitle_form="tracks", **both).json()
        assert default == explicit
        assert default["subtitle_form"] == "tracks"
        assert [track["language"] for track in default["tracks"]] == ["zh-TW", "en"]
        job = client.post(
            f"/api/videos/{video_id}/exports", json={"snapshot": default}, headers=ORIGIN
        ).json()
        assert wait_job(client, job["id"])["status"] == "completed"
        assert client.get(f"/api/videos/{video_id}").json()["exports"][0]["subtitle_form"] == (
            "tracks"
        )
        assert preview(client, video_id, target, subtitle_form="burned", **both).status_code == 400
        mkv = preview(client, video_id, target, subtitle_form="burned", container="mkv")
        assert mkv.status_code == 400
        tampered = {**default, "subtitle_form": "burned"}
        assert (
            client.post(
                f"/api/videos/{video_id}/exports", json={"snapshot": tampered}, headers=ORIGIN
            ).status_code
            == 400
        )


def test_migration_marks_existing_artifacts_as_tracks(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    adapter = FakeAdapter()
    video = library.import_video("abcdefghijk", "old", 1, "url", adapter.source.model_dump_json())
    with sqlite3.connect(library.db_path) as connection:
        connection.execute("ALTER TABLE export_artifacts DROP COLUMN subtitle_form")
        connection.execute(
            "INSERT INTO jobs (id,kind,status,created_at,updated_at,video_id) "
            "VALUES ('job','export','completed','now','now',?)",
            (video["id"],),
        )
        connection.execute(
            "INSERT INTO export_artifacts VALUES (?,?,?,?,?,?,?)",
            ("old", video["id"], "job", "videos/x/exports/old.mp4", "mp4", "{}", "c"),
        )
        connection.execute("PRAGMA user_version=5")
    settings = replace(load_settings(tmp_path), library_dir=library.root)
    with TestClient(create_app(settings, adapter), base_url="http://127.0.0.1:8765") as c:
        artifacts = c.get(f"/api/videos/{video['id']}").json()["exports"]
    assert [(a["id"], a["subtitle_form"]) for a in artifacts] == [("old", "tracks")]


def test_long_lines_wrap_within_frame_and_text_cannot_inject_tags() -> None:
    layout = SubtitleLayout(640, 360)
    long_cjk = "這是一段很長的繁體中文字幕用來測試自動換行是否正常運作而且不會超出畫面範圍"
    lines = wrap_line(long_cjk, layout.line_width)
    assert len(lines) > 1 and "".join(lines) == long_cjk
    assert all(len(line) <= layout.line_width for line in lines)
    words = wrap_line("word " * 40, layout.line_width)
    assert len(words) > 1 and all(not line.startswith(" ") for line in words)
    assert all(len(line.rstrip()) * 0.5 <= layout.line_width for line in words)
    # Closing punctuation stays on the previous line.
    assert not any(line.startswith("，") for line in wrap_line("字" * 31 + "，尾", 32))
    ass = render_ass(
        [Cue(id="x", start=1.234, end=1.236, text="{\\b1}bold\\Nnot\nnext")],
        "en",
        layout,
    )
    dialogue = ass.splitlines()[-1]
    assert dialogue.startswith("Dialogue: 0,0:00:01.23,0:00:01.24,")
    assert dialogue.endswith("\\{＼b1\\}bold＼Nnot\\Nnext")


def _vt_available() -> bool:
    if shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None:
        return False
    try:
        MediaExporter().require_hardware(Event())
    except SourceError:
        return False
    return True


def _gray_frame(path: Path, seconds: float) -> bytes:
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", str(seconds), "-i", str(path)]
        + ["-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "gray", "-"],
        check=True,
        capture_output=True,
    ).stdout


def _difference(a: bytes, b: bytes, width: int, rows: range, columns: range) -> float:
    total = sum(abs(a[y * width + x] - b[y * width + x]) for y in rows for x in columns)
    return total / (len(rows) * len(columns))


@pytest.mark.skipif(not _vt_available(), reason="ffmpeg with VideoToolbox H.264 is unavailable")
@pytest.mark.parametrize(
    ("container", "audio", "audio_args"),
    [("mp4", "aac", ["-c:a", "aac"]), ("mkv", "opus", ["-c:a", "libopus"])],
)
def test_burned_export_real_ffmpeg(
    tmp_path: Path, container: str, audio: str, audio_args: list[str]
) -> None:
    width, height = 640, 360
    # Characters that need filtergraph escaping must not break the subtitles filter path.
    directory = tmp_path / "it's: a,[dir];"
    directory.mkdir()
    source = directory / f"source.{container}"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i"]
        + [f"color=c=0x336699:s={width}x{height}:r=25", "-f", "lavfi", "-i"]
        + ["sine=f=440:r=48000", "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
        + audio_args
        + [str(source)],
        check=True,
        capture_output=True,
    )
    text = "這是一段很長的繁體中文字幕用來測試自動換行是否正常運作而且不會超出畫面範圍"
    track = ExportTrack(
        version_id="target",
        language="zh-TW",
        name="target",
        srt="",
        cues=(Cue(id="one", start=1.0, end=2.0, text=text),),
    )
    reported: list[float | None] = []
    output = MediaExporter().burn(source, track, directory / "output", Event(), reported.append)
    assert reported and reported[0] == 0.0
    info = json.loads(
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(output)]
        )
    )
    assert output.suffix == ".mp4"
    assert "mp4" in info["format"]["format_name"].split(",")
    streams = {s["codec_type"]: s for s in info["streams"]}
    assert set(streams) == {"video", "audio"}
    assert streams["video"]["codec_name"] == "h264"
    assert (streams["video"]["width"], streams["video"]["height"]) == (width, height)
    assert streams["audio"]["codec_name"] == "aac"
    rows, columns = range(height), range(width)
    bottom = range(height * 2 // 3, height)
    for seconds, inside in ((0.5, False), (1.5, True), (2.5, False)):
        original, burned = _gray_frame(source, seconds), _gray_frame(output, seconds)
        if inside:
            assert _difference(original, burned, width, bottom, columns) > 5
            # Wrapped lines stay inside the side margins.
            for edge in (range(0, 8), range(width - 8, width)):
                assert _difference(original, burned, width, bottom, edge) < 1
        else:
            assert _difference(original, burned, width, rows, columns) < 1


def test_reexport_panel_offers_burned_subtitle_form() -> None:
    static = Path(exports.__file__).parent / "static"
    page = (static / "index.html").read_text()
    script = (static / "workspace.js").read_text()
    assert 'id="export-form"' in page
    assert '<option value="burned">' in page and '<option value="tracks">' in page
    assert 'subtitle_form: element("export-form").value' in script
    assert "hardware_encoder_unavailable" in script
