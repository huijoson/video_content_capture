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
from video_content_capture.workspace.burned import (
    BAND_GAP_SCALE,
    LINE_SPACING,
    ORIGINAL_ADVANCE,
    ORIGINAL_STYLE,
    TARGET_STYLE,
    SubtitleLayout,
    render_ass,
    wrap_line,
)
from video_content_capture.workspace.config import load_settings
from video_content_capture.workspace.exports import ExportTrack, MediaExporter
from video_content_capture.workspace.storage import Library
from video_content_capture.workspace.subtitles import Cue
from video_content_capture.workspace.youtube import SourceError

ORIGIN = {"Origin": "http://127.0.0.1:8765"}

# Long enough to wrap to several rendered lines at every resolution, at the original's
# own font size, so it exercises the multi-line band reservation below the target.
WRAPPED_ORIGINAL = (
    "the storm will pass before the river reaches the door, and the lights will come back on "
    "again, or so I kept telling myself while the wind shook the glass"
)


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
        # Burned bilingual is still one MP4; MKV and a missing original are refused.
        mkv = preview(client, video_id, target, subtitle_form="burned", container="mkv")
        assert mkv.status_code == 400
        assert (
            preview(
                client, video_id, target, subtitle_form="burned", include_original=True
            ).status_code
            == 400
        )
        tampered = {**default, "subtitle_form": "burned"}
        # Tampering the form on an MKV tracks snapshot is still rejected by the schema.
        assert (
            client.post(
                f"/api/videos/{video_id}/exports",
                json={"snapshot": {**tampered, "container": "mkv"}},
                headers=ORIGIN,
            ).status_code
            == 400
        )
        # A burned snapshot cannot smuggle in a third track.
        three = {
            **tampered,
            "tracks": default["tracks"] + [default["tracks"][0]],
        }
        assert (
            client.post(
                f"/api/videos/{video_id}/exports", json={"snapshot": three}, headers=ORIGIN
            ).status_code
            == 400
        )


def test_bilingual_burned_export_stacks_target_above_smaller_original(tmp_path: Path) -> None:
    runner = FakeFFmpeg()
    client, library, video_id, _, target = make_client(tmp_path, runner)
    # The original version keeps its own cue times, different from the target's.
    original = library.create_subtitle_version(
        video_id,
        "en",
        "original offset",
        "import",
        [Cue(id="two", start=0.2, end=0.7, text="the original line")],
    )
    with client:
        response = preview(
            client,
            video_id,
            target,
            include_original=True,
            original_version_id=original["id"],
            subtitle_form="burned",
        )
        assert response.status_code == 200
        snapshot = response.json()
        assert snapshot["include_original"] is True
        assert snapshot["subtitle_form"] == "burned"
        assert [track["version_id"] for track in snapshot["tracks"]] == [
            target["id"],
            original["id"],
        ]
        job = client.post(
            f"/api/videos/{video_id}/exports", json={"snapshot": snapshot}, headers=ORIGIN
        ).json()
        assert wait_job(client, job["id"])["status"] == "completed"
        artifact = client.get(f"/api/videos/{video_id}").json()["exports"][0]
        assert artifact["subtitle_form"] == "burned"
        # The artifact snapshot keeps the bilingual flag for reproducibility.
        assert json.loads(artifact["summary"]) == snapshot
    ass = runner.subtitles[-1]
    styles = {
        line.removeprefix("Style: ").split(",")[0]: line.removeprefix("Style: ").split(",")
        for line in ass.splitlines()
        if line.startswith("Style: ")
    }
    assert set(styles) == {"BilingualTarget", "BilingualOriginal"}
    # The target line is bigger and sits above the original line in the same frame.
    assert int(styles["BilingualTarget"][2]) > int(styles["BilingualOriginal"][2])
    assert int(styles["BilingualTarget"][21]) > int(styles["BilingualOriginal"][21])
    assert styles["BilingualTarget"][3] == "&H00FFFFFF"  # white text, black edge
    assert styles["BilingualTarget"][5] == "&H00000000"
    assert styles["BilingualTarget"][18] == styles["BilingualOriginal"][18] == "2"
    assert styles["BilingualTarget"][1] == "Heiti TC"  # zh-TW target
    assert styles["BilingualOriginal"][1] == "Hiragino Sans GB"  # English original
    # Each version keeps its own cue times: nothing is merged or re-timed.
    dialogues = [line for line in ass.splitlines() if line.startswith("Dialogue: ")]
    assert dialogues == [
        "Dialogue: 1,0:00:00.00,0:00:00.50,BilingualTarget,,0,0,0,,original",
        "Dialogue: 0,0:00:00.20,0:00:00.70,BilingualOriginal,,0,0,0,,the original line",
    ]


def test_burned_same_version_on_both_sides_renders_one_line(tmp_path: Path) -> None:
    runner = FakeFFmpeg()
    client, _, video_id, _, target = make_client(tmp_path, runner)
    with client:
        response = preview(
            client,
            video_id,
            target,
            include_original=True,
            original_version_id=target["id"],
            subtitle_form="burned",
        )
        assert response.status_code == 200
        snapshot = response.json()
        assert [track["version_id"] for track in snapshot["tracks"]] == [target["id"]]
        job = client.post(
            f"/api/videos/{video_id}/exports", json={"snapshot": snapshot}, headers=ORIGIN
        ).json()
        assert wait_job(client, job["id"])["status"] == "completed"
    ass = runner.subtitles[-1]
    assert "BilingualTarget" not in ass and "BilingualOriginal" not in ass
    assert [line for line in ass.splitlines() if line.startswith("Dialogue: ")] == [
        "Dialogue: 0,0:00:00.00,0:00:00.50,Default,,0,0,0,,original"
    ]


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


def _ass_styles(ass: str) -> dict[str, list[str]]:
    return {
        fields[0]: fields
        for line in ass.splitlines()
        if line.startswith("Style: ")
        for fields in [line.removeprefix("Style: ").split(",")]
    }


def test_bilingual_target_margin_reserves_wrapped_original_block() -> None:
    """A wrapped original must never be lifted above the target by collision avoidance."""
    texts = [
        "orig text",
        WRAPPED_ORIGINAL,
        " ".join(["word"] * 40),
        "這是一段很長的繁體中文字幕用來測試自動換行是否正常運作而且不會超出畫面範圍並持續延伸下去",
    ]
    for width, height in ((640, 360), (1280, 720), (1920, 1080)):
        layout = SubtitleLayout(width, height)
        # The original wraps at its own font size, so it keeps more text per line.
        assert layout.original_line_width > layout.line_width
        counts: set[int] = set()
        margins: dict[int, int] = {}
        for text in texts:
            for lines in (1, 2, 3):
                cues = (Cue(id="two", start=1.0, end=2.0, text="\n".join([text] * lines)),)
                ass = render_ass(cues, "zh-TW", layout, original=(cues, "en"))
                styles = _ass_styles(ass)
                assert set(styles) == {"BilingualTarget", "BilingualOriginal"}
                rendered = sum(
                    len(wrap_line(part, layout.original_line_width, ORIGINAL_ADVANCE))
                    for part in [text] * lines
                )
                counts.add(rendered)
                target, bottom = (
                    int(styles["BilingualTarget"][21]),
                    int(styles["BilingualOriginal"][21]),
                )
                block = rendered * round(layout.original_font_size * LINE_SPACING)
                gap = round(layout.original_font_size * BAND_GAP_SCALE)
                if layout.margin_vertical + block + gap <= layout.margin_limit:
                    # The reservation covers every rendered line at the pitch libass
                    # actually uses (measured 1.0 em), so the target stays above the block.
                    assert rendered * layout.original_font_size <= target - bottom
                else:
                    # A block taller than the cap cannot be reserved; the margin stops
                    # there so the target stays on screen (see the clamp test below).
                    assert target == layout.margin_limit
                    assert layout.margin_vertical + block + gap > target
                assert LINE_SPACING >= 1.0
                # The hard-coded single-line band #5 first shipped would not have covered it.
                if rendered > 1 and target < layout.margin_limit:
                    assert target > bottom + round(layout.original_font_size * LINE_SPACING)
                # The original keeps its own style: smaller font, its own bottom margin.
                assert styles["BilingualOriginal"][2] == str(layout.original_font_size)
                assert bottom == layout.margin_vertical
                margins[rendered] = target
                # Wrapping never lets subtitle text inject an ASS override tag.
                assert "\\pos" not in ass and "{" not in ass.replace("\\{", "")
        # The band covers single-line and multi-line originals alike.
        assert {1, 2} <= counts and max(counts) >= 3
        # The reserved band grows with the original's rendered line count, up to the cap.
        ordered = sorted(margins)
        assert margins[ordered[0]] < margins[ordered[-1]]
        assert margins[1] < margins[2]
        assert all(
            margins[smaller] <= margins[larger]
            for smaller, larger in zip(ordered, ordered[1:], strict=False)
        )


def test_bilingual_margin_clamp_keeps_target_on_screen() -> None:
    """One enormous original cue must not reserve the target off the top of the frame."""
    for width, height in ((640, 360), (1280, 720), (1920, 1080), (1080, 1920)):
        layout = SubtitleLayout(width, height)
        limit = layout.margin_limit
        # The cap leaves the target the upper half of the frame, always inside it.
        assert limit < height
        assert limit == round(height * 0.5)
        # Normal originals are nowhere near it, so they keep the exact reservation.
        for lines in (1, 2, 4):
            assert layout.bilingual_margin_vertical(lines) < limit
            assert layout.bilingual_margin_vertical(lines) == (
                layout.margin_vertical
                + lines * round(layout.original_font_size * LINE_SPACING)
                + round(layout.original_font_size * BAND_GAP_SCALE)
            )
        # Past the cap the margin stops growing, however tall the original gets.
        assert layout.bilingual_margin_vertical(500) == limit
        big = layout.bilingual_margin_vertical(500)
        huge = layout.bilingual_margin_vertical(5_000)
        assert big == huge == limit
        # A single cue of 1200 CJK characters really does reach the cap.
        text = "字" * 1200
        cue = Cue(id="big", start=1.0, end=2.0, text=text)
        ass = render_ass([cue], "zh-TW", layout, original=([cue], "en"))
        styles = _ass_styles(ass)
        assert int(styles["BilingualTarget"][21]) == limit
        # The target's own line still fits below the cap with room for its own height.
        assert limit + layout.font_size < height
        assert styles["BilingualOriginal"][21] == str(layout.margin_vertical)
        # The clamp is a style-level margin: no injected positioning reaches the text.
        assert "\\pos" not in ass and "{" not in ass.replace("\\{", "")


def test_bilingual_target_uses_its_own_ass_layer() -> None:
    """The target's layer, not the reservation, is what pins it above the original."""
    layout = SubtitleLayout(640, 360)
    cues = (Cue(id="one", start=1.0, end=2.0, text="繁體中文字幕"),)
    original = (Cue(id="two", start=1.0, end=2.0, text=WRAPPED_ORIGINAL),)
    ass = render_ass(cues, "zh-TW", layout, original=(original, "en"))
    dialogues = [line for line in ass.splitlines() if line.startswith("Dialogue: ")]
    assert len(dialogues) == 2
    # libass only lifts events within one layer, so the original cannot displace the
    # target whatever the original's real line metrics turn out to be.
    assert dialogues[0].startswith("Dialogue: 1,") and TARGET_STYLE in dialogues[0]
    assert dialogues[1].startswith("Dialogue: 0,") and ORIGINAL_STYLE in dialogues[1]
    # The single-language path keeps every event on the default layer.
    single = render_ass(cues, "zh-TW", layout)
    assert all(
        line.startswith("Dialogue: 0,")
        for line in single.splitlines()
        if line.startswith("Dialogue: ")
    )


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


def _mask(source: bytes, frame: bytes, threshold: int = 30) -> bytearray:
    """Pixels the burned frame added over the flat source colour: the subtitle ink."""
    return bytearray(
        1 if abs(before - after) > threshold else 0
        for before, after in zip(source, frame, strict=True)
    )


def _bands(mask: bytearray, width: int, height: int, max_gap: int = 8) -> list[range]:
    """Ink bands top to bottom; lines closer than max_gap rows belong to one band."""
    bands: list[range] = []
    start: int | None = None
    gap = 0
    for row in range(height):
        if any(mask[row * width : (row + 1) * width]):
            if start is None:
                start = row
            gap = 0
        elif start is not None:
            gap += 1
            if gap > max_gap:
                bands.append(range(start, row - gap + 1))
                start = None
    if start is not None:
        bands.append(range(start, height))
    return bands


def _ink_columns(mask: bytearray, width: int, rows: range) -> range:
    """Horizontal extent of the glyphs in a band."""
    columns = [x for x in range(width) if any(mask[y * width + x] for y in rows)]
    assert columns, "the band has no ink"
    return range(columns[0], columns[-1] + 1)


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


def _burn_real(
    tmp_path: Path,
    name: str,
    *,
    target: ExportTrack,
    original: ExportTrack | None,
    width: int = 640,
    height: int = 360,
) -> tuple[Path, Path]:
    """A short flat-colour MP4 plus the real burned output for one subtitle set."""
    source = tmp_path / f"source-{name}.mp4"
    subprocess.run(
        ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i"]
        + [f"color=c=0x336699:s={width}x{height}:r=25", "-f", "lavfi", "-i"]
        + ["sine=f=440:r=48000", "-t", "3", "-c:v", "libx264", "-pix_fmt", "yuv420p"]
        + ["-c:a", "aac", str(source)],
        check=True,
        capture_output=True,
    )
    output = MediaExporter().burn(
        source, target, tmp_path / f"burn-{name}", Event(), original=original
    )
    return source, output


def _bands_at(
    source: Path, frame_path: Path, seconds: float, width: int, height: int, max_gap: int = 2
) -> list[range]:
    mask = _mask(_gray_frame(source, seconds), _gray_frame(frame_path, seconds))
    return _bands(mask, width, height, max_gap)


@pytest.mark.skipif(not _vt_available(), reason="ffmpeg with VideoToolbox H.264 is unavailable")
def test_bilingual_burn_real_ffmpeg_stacks_target_above_smaller_original(tmp_path: Path) -> None:
    width, height = 640, 360
    target = ExportTrack(
        version_id="target",
        language="zh-TW",
        name="target",
        srt="",
        # Wide glyphs, so the target line is clearly the wider band of the two.
        cues=(Cue(id="one", start=1.0, end=2.0, text="繁體中文字幕"),),
    )
    original = ExportTrack(
        version_id="original",
        language="en",
        name="original",
        srt="",
        # Its own timing, offset from the target's: cues are never merged or re-timed.
        cues=(Cue(id="two", start=1.6, end=2.4, text="orig text"),),
    )
    target_source, target_burn = _burn_real(tmp_path, "target", target=target, original=None)
    both_source, both_burn = _burn_real(tmp_path, "both", target=target, original=original)

    # The single-language burn gives the target text its own glyph signature.
    alone = _bands_at(target_source, target_burn, 1.3, width, height)
    assert len(alone) == 1, alone
    alone_frame = _mask(_gray_frame(target_source, 1.3), _gray_frame(target_burn, 1.3))
    alone_span = _ink_columns(alone_frame, width, alone[0])

    # Both bands render at once, target on top and the smaller original below it.
    bands = _bands_at(both_source, both_burn, 1.8, width, height)
    assert len(bands) == 2, bands
    top, bottom = bands
    assert top.stop <= bottom.start
    frame = _mask(_gray_frame(both_source, 1.8), _gray_frame(both_burn, 1.8))
    top_span = _ink_columns(frame, width, top)
    # The top band holds the target text: the same glyph run as the target-only burn.
    assert abs(top_span.start - alone_span.start) <= 4
    assert abs(top_span.stop - alone_span.stop) <= 4
    assert abs(len(top) - len(alone[0])) <= 4
    # The bottom band is the original in a smaller font: shorter band, narrower text.
    assert len(bottom) < len(top)
    bottom_span = _ink_columns(frame, width, bottom)
    assert bottom_span.stop - bottom_span.start < top_span.stop - top_span.start
    # The target alone still renders in the same place as in the single-language burn.
    only_target = _bands_at(both_source, both_burn, 1.3, width, height)
    assert len(only_target) == 1, only_target
    target_frame = _mask(_gray_frame(both_source, 1.3), _gray_frame(both_burn, 1.3))
    target_span = _ink_columns(target_frame, width, only_target[0])
    assert abs(target_span.start - top_span.start) <= 2
    assert abs(target_span.stop - top_span.stop) <= 2
    # Each version keeps its own timing: at 2.2 s only the original is left, in place.
    tail = _bands_at(both_source, both_burn, 2.2, width, height)
    assert len(tail) == 1, tail
    tail_frame = _mask(_gray_frame(both_source, 2.2), _gray_frame(both_burn, 2.2))
    tail_span = _ink_columns(tail_frame, width, tail[0])
    assert abs(tail_span.start - bottom_span.start) <= 2
    assert abs(tail_span.stop - bottom_span.stop) <= 2
    # Outside every cue window the burned picture stays identical to the source.
    assert _bands_at(both_source, both_burn, 0.5, width, height) == []


@pytest.mark.skipif(not _vt_available(), reason="ffmpeg with VideoToolbox H.264 is unavailable")
@pytest.mark.parametrize(("width", "height"), [(640, 360), (1920, 1080)])
def test_bilingual_burn_real_ffmpeg_keeps_wrapped_original_below_target(
    tmp_path: Path, width: int, height: int
) -> None:
    """A multi-line original must not be lifted above the target by collision avoidance."""
    layout = SubtitleLayout(width, height)
    target = ExportTrack(
        version_id="target",
        language="zh-TW",
        name="target",
        srt="",
        cues=(Cue(id="one", start=1.0, end=2.0, text="繁體中文字幕"),),
    )
    original = ExportTrack(
        version_id="original",
        language="en",
        name="original",
        srt="",
        # Its own timing, and long enough to wrap to several rendered lines.
        cues=(Cue(id="two", start=0.6, end=2.4, text=WRAPPED_ORIGINAL),),
    )
    alone_source, alone_burn = _burn_real(
        tmp_path, f"alone-{width}", target=target, original=None, width=width, height=height
    )
    both_source, both_burn = _burn_real(
        tmp_path, f"both-{width}", target=target, original=original, width=width, height=height
    )

    # The single-language burn gives the target text its own glyph signature.
    alone_band = _bands_at(alone_source, alone_burn, 1.5, width, height)[0]
    alone = _mask(_gray_frame(alone_source, 1.5), _gray_frame(alone_burn, 1.5))

    # The original wraps to several rendered lines, the case that used to collide.
    wrapped_lines = wrap_line(WRAPPED_ORIGINAL, layout.original_line_width, ORIGINAL_ADVANCE)
    assert len(wrapped_lines) >= 2, wrapped_lines

    # While both cues are live the target sits on top and the original block below it.
    blocks = _bands_at(both_source, both_burn, 1.5, width, height, max_gap=8)
    assert len(blocks) == 2, blocks
    top, below = blocks
    assert top.stop <= below.start, blocks
    frame = _mask(_gray_frame(both_source, 1.5), _gray_frame(both_burn, 1.5))
    # The top band holds the target text: the same glyph run as the target-only burn.
    top_span = _ink_columns(frame, width, top)
    alone_span = _ink_columns(alone, width, alone_band)
    assert abs(top_span.start - alone_span.start) <= 4
    assert abs(top_span.stop - alone_span.stop) <= 4
    assert abs(len(top) - len(alone_band)) <= 4
    # The bottom band spans several line pitches, so the original really did wrap.
    assert len(below) >= (len(wrapped_lines) - 1) * layout.original_font_size + 1, below
    # A single target line cannot account for that height.
    assert len(below) > len(top)
    # The target sits exactly in the band reserved for it, so collision avoidance
    # never had to lift it: a short band would mean the original pushed it upward.
    target_margin = layout.bilingual_margin_vertical(len(wrapped_lines))
    assert abs((height - top.stop) - target_margin) <= 6, (top, target_margin)

    # The original keeps its own place: its block bottom sits at its own bottom margin.
    assert abs((height - below.stop) - layout.margin_vertical) <= 6, below

    # When the target cue ends the original neither moves nor jumps.
    after = _bands_at(both_source, both_burn, 2.2, width, height, max_gap=8)
    assert len(after) == 1, after
    assert after[0] == below
    assert _bands_at(both_source, both_burn, 0.5, width, height) == []


@pytest.mark.skipif(not _vt_available(), reason="ffmpeg with VideoToolbox H.264 is unavailable")
def test_bilingual_burn_real_ffmpeg_keeps_target_visible_past_the_margin_cap(
    tmp_path: Path,
) -> None:
    """An original too tall to reserve must not push the target out of the picture."""
    width, height = 1920, 1080
    layout = SubtitleLayout(width, height)
    target = ExportTrack(
        version_id="target",
        language="zh-TW",
        name="target",
        srt="",
        cues=(Cue(id="one", start=1.5, end=2.9, text="繁體中文字幕"),),
    )
    original = ExportTrack(
        version_id="original",
        language="zh-CN",
        name="original",
        srt="",
        cues=(
            # Far more text than the frame can hold: it alone would reserve the target
            # right off the top of the picture. Then a short cue in its own place.
            Cue(id="two", start=1.0, end=2.4, text="字" * 1200),
            Cue(id="three", start=2.5, end=2.9, text="原文短句"),
        ),
    )
    alone_source, alone_burn = _burn_real(
        tmp_path, "cap-alone", target=target, original=None, width=width, height=height
    )
    both_source, both_burn = _burn_real(
        tmp_path, "cap-both", target=target, original=original, width=width, height=height
    )

    alone_band = _bands_at(alone_source, alone_burn, 1.6, width, height)[0]
    alone = _mask(_gray_frame(alone_source, 1.6), _gray_frame(alone_burn, 1.6))
    alone_span = _ink_columns(alone, width, alone_band)

    # Past 2.5 s the oversized original is gone but its reservation is not: the target
    # and the short original line both render, so they can be told apart.
    bands = _bands_at(both_source, both_burn, 2.7, width, height)
    assert len(bands) == 2, bands
    top, bottom = bands
    assert top.stop <= bottom.start, bands
    frame = _mask(_gray_frame(both_source, 2.7), _gray_frame(both_burn, 2.7))
    top_span = _ink_columns(frame, width, top)
    # The target's glyph run is on screen, unclipped and unshifted, at the capped margin.
    assert abs(top_span.start - alone_span.start) <= 4
    assert abs(top_span.stop - alone_span.stop) <= 4
    assert abs(len(top) - len(alone_band)) <= 4
    assert top.start > 0 and top.stop <= height
    assert abs((height - top.stop) - layout.margin_limit) <= 6, (top, layout.margin_limit)
    # The short original keeps its own, much lower place below the capped target.
    assert abs((height - bottom.stop) - layout.margin_vertical) <= 6, bottom
    # The reservation that original asked for is off the frame: the cap is what keeps
    # the target visible instead of letting it be pushed out of the picture.
    rendered = len(wrap_line("字" * 1200, layout.original_line_width, ORIGINAL_ADVANCE))
    assert rendered > 20
    assert layout.bilingual_margin_vertical(rendered) == layout.margin_limit
    assert (
        layout.margin_vertical
        + rendered * round(layout.original_font_size * LINE_SPACING)
        + round(layout.original_font_size * BAND_GAP_SCALE)
        > height
    )
    # Outside every cue window the burned picture stays identical to the source.
    assert _bands_at(both_source, both_burn, 0.5, width, height) == []
    # While the oversized original is live it keeps its own bottom margin: with the two
    # versions on separate ASS layers it overflows the top edge instead of being packed
    # above the target (which is what would move it when a target cue ends).
    live = _mask(_gray_frame(both_source, 1.6), _gray_frame(both_burn, 1.6))
    near_bottom = range(height - layout.margin_vertical - 2, height)
    assert any(live[y * width : (y + 1) * width] for y in near_bottom)


def test_reexport_panel_offers_burned_subtitle_form() -> None:
    static = Path(exports.__file__).parent / "static"
    page = (static / "index.html").read_text()
    script = (static / "workspace.js").read_text()
    assert 'id="export-form"' in page
    assert '<option value="burned">' in page and '<option value="tracks">' in page
    assert 'subtitle_form: element("export-form").value' in script
    assert "hardware_encoder_unavailable" in script
    # The export panel offers the bilingual burned toggle instead of forcing it off.
    assert 'id="include-original"' in page and "雙語" in page
    assert "目標在上、原文在下" in script
    assert 'element("include-original").checked = false' not in script.split("updateExportForm")[1]
