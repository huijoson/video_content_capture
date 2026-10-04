"""S5 compatible preview: real ffmpeg encode/probe, cancellation and publication gate."""

import json
import os
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from threading import Event

import pytest

from video_content_capture.workspace.jobs import checksum
from video_content_capture.workspace.previews import PreviewEncoder, PreviewService, moov_first
from video_content_capture.workspace.storage import Library
from video_content_capture.workspace.subtitles import Cue
from video_content_capture.workspace.youtube import SourceError, run_process

needs_ffmpeg = pytest.mark.skipif(
    shutil.which("ffmpeg") is None or shutil.which("ffprobe") is None,
    reason="ffmpeg/ffprobe not installed",
)


def synthesize(path: Path, size: str, video: list[str], audio: list[str] | None) -> Path:
    command = ["ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", f"testsrc=s={size}:r=10"]
    if audio is not None:
        command += ["-f", "lavfi", "-i", "sine=f=440:r=48000"]
    command += ["-t", "0.5", *video]
    if audio is not None:
        command += audio
    subprocess.run([*command, str(path)], check=True, capture_output=True)
    return path


def probe(path: Path) -> dict:
    return json.loads(
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_streams", "-show_format", "-of", "json", str(path)]
        )
    )


@needs_ffmpeg
def test_vp9_opus_source_becomes_faststart_h264_aac_without_upscaling(tmp_path: Path) -> None:
    source = synthesize(
        tmp_path / "source.webm",
        "320x240",
        ["-c:v", "libvpx-vp9", "-deadline", "realtime", "-cpu-used", "8"],
        ["-c:a", "libopus"],
    )
    before = source.read_bytes()
    output = PreviewEncoder().encode(source, tmp_path / "preview", Event())
    info = probe(output)
    streams = {stream["codec_type"]: stream for stream in info["streams"]}
    assert streams["video"]["codec_name"] == "h264"
    assert streams["video"]["height"] == 240  # never upscaled to 720
    assert streams["video"]["pix_fmt"] == "yuv420p"
    assert streams["audio"]["codec_name"] == "aac"
    assert "mp4" in info["format"]["format_name"]
    assert moov_first(output)
    assert source.read_bytes() == before
    assert output.parent == tmp_path / "preview"


@needs_ffmpeg
def test_tall_source_is_limited_to_720p_and_silent_source_has_no_audio(tmp_path: Path) -> None:
    source = synthesize(tmp_path / "source.mkv", "1920x1080", ["-c:v", "ffv1"], None)
    output = PreviewEncoder().encode(source, tmp_path / "preview", Event())
    streams = probe(output)["streams"]
    video = [s for s in streams if s["codec_type"] == "video"]
    assert [(s["width"], s["height"]) for s in video] == [(1280, 720)]
    assert [s for s in streams if s["codec_type"] == "audio"] == []


def test_moov_first_detects_non_faststart_layout(tmp_path: Path) -> None:
    def box(kind: bytes, payload: bytes = b"") -> bytes:
        return (8 + len(payload)).to_bytes(4, "big") + kind + payload

    fast = tmp_path / "fast.mp4"
    fast.write_bytes(box(b"ftyp", b"isom") + box(b"moov") + box(b"mdat", b"x"))
    slow = tmp_path / "slow.mp4"
    slow.write_bytes(box(b"ftyp", b"isom") + box(b"mdat", b"x") + box(b"moov"))
    broken = tmp_path / "broken.mp4"
    broken.write_bytes(b"\x00\x00")
    assert moov_first(fast)
    assert not moov_first(slow)
    assert not moov_first(broken)


def test_encoder_failure_maps_to_preview_failed_without_details(tmp_path: Path) -> None:
    def runner(args, cancel):
        if args[0] == "ffprobe":
            return json.dumps({"streams": [{"codec_type": "video", "height": 480}]})
        raise SourceError("source_unavailable", "/secret/path stderr")

    with pytest.raises(SourceError) as raised:
        PreviewEncoder(runner).encode(tmp_path / "source.mkv", tmp_path / "out", Event())
    assert raised.value.code == "preview_failed"
    assert "/secret" not in str(raised.value)


def test_encoder_builds_720p_h264_aac_faststart_command(tmp_path: Path) -> None:
    calls: list[list[str]] = []

    def runner(args, cancel):
        calls.append(args)
        if args[0] == "ffprobe":
            return json.dumps(
                {"streams": [{"codec_type": "video", "height": 2160}, {"codec_type": "audio"}]}
            )
        raise SourceError("source_unavailable", "stop after command")

    with pytest.raises(SourceError):
        PreviewEncoder(runner).encode(tmp_path / "source.mkv", tmp_path / "out", Event())
    command = calls[1]
    assert command[0] == "ffmpeg"
    assert command[command.index("-vf") + 1] == "scale=-2:720"
    assert command[command.index("-c:v") + 1] == "libx264"
    assert command[command.index("-c:a") + 1] == "aac"
    assert "+faststart" in command
    assert "-n" in command  # never overwrite an existing file
    assert command[-1].startswith(str(tmp_path / "out"))
    assert (tmp_path / "out").is_dir()  # the boundary creates its own output parent


def test_cancel_terminates_the_encoder_subprocess(tmp_path: Path) -> None:
    pid_file = tmp_path / "pid"
    child = (
        "import os, pathlib, time; "
        f"pathlib.Path({str(pid_file)!r}).write_text(str(os.getpid())); time.sleep(30)"
    )

    def runner(args, cancel):
        if args[0] == "ffprobe":
            return json.dumps({"streams": [{"codec_type": "video", "height": 480}]})
        return run_process([sys.executable, "-c", child], cancel)

    cancel = Event()

    def stop_when_started() -> None:
        deadline = time.monotonic() + 10
        while not pid_file.exists() and time.monotonic() < deadline:
            time.sleep(0.01)
        cancel.set()

    threading.Thread(target=stop_when_started).start()
    started = time.monotonic()
    with pytest.raises(SourceError) as raised:
        PreviewEncoder(runner).encode(tmp_path / "source.mkv", tmp_path / "out", cancel)
    assert raised.value.code == "cancelled"
    assert time.monotonic() - started < 10
    pid = int(pid_file.read_text())
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        try:
            os.kill(pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail("encoder subprocess survived cancellation")


class FakeEncoder:
    def __init__(self) -> None:
        self.calls = 0
        self.fail = False
        self.on_encode = lambda: None

    def encode(self, source: Path, directory: Path, cancel: Event) -> Path:
        self.calls += 1
        self.on_encode()
        if self.fail:
            raise SourceError("preview_failed", "相容預覽製作失敗")
        directory.mkdir(parents=True, exist_ok=True)
        output = directory / "preview.mp4"
        output.write_bytes(b"preview-bytes")
        return output


def setup_preview(tmp_path: Path, playable: bool = False):
    library = Library(tmp_path / "library")
    library.initialize()
    video = library.import_video("abcdefghijk", "影片", 10, "https://youtu.be/abcdefghijk", "{}")
    video_id = str(video["id"])
    root = library.video_dir(video_id)
    (root / "source" / "asset.mkv").write_bytes(b"source")
    (root / "exports" / "done.mp4").write_bytes(b"export")
    with library._connect() as connection:
        connection.execute(
            "INSERT INTO media_assets VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "asset",
                video_id,
                "v",
                "a",
                f"videos/{video_id}/source/asset.mkv",
                checksum(root / "source" / "asset.mkv"),
                "fp",
                int(playable),
                "mp4" if playable else "mkv",
            ),
        )
    source = library.create_subtitle_version(
        video_id, "en", "Original", "import", [Cue(id="1", start=0, end=1, text="Hi")]
    )
    encoder = FakeEncoder()
    return library, video_id, encoder, PreviewService(library, encoder), source


def test_playable_source_never_creates_a_preview_job(tmp_path: Path) -> None:
    library, video_id, encoder, service, _ = setup_preview(tmp_path, playable=True)
    with pytest.raises(ValueError):
        service.create(video_id, "asset")
    assert library.list_jobs() == []
    assert encoder.calls == 0


def test_unplayable_source_publishes_separate_preview_without_touching_artifacts(
    tmp_path: Path,
) -> None:
    library, video_id, encoder, service, source = setup_preview(tmp_path)
    job = service.create(video_id, "asset")
    assert job["kind"] == "preview"
    assert service.create(video_id, "asset")["id"] == job["id"]  # no duplicate active job
    service.run(job, library.start_job(str(job["id"])), Event())
    assert library.get_job(str(job["id"]))["status"] == "completed"
    previews = library.previews(video_id)
    assert len(previews) == 1 and previews[0]["asset_id"] == "asset"
    relative = Path(str(previews[0]["path"]))
    assert relative.parts[:3] == ("videos", video_id, "previews")
    assert (library.root / relative).read_bytes() == b"preview-bytes"
    root = library.video_dir(video_id)
    assert (root / "source" / "asset.mkv").read_bytes() == b"source"
    assert (root / "exports" / "done.mp4").read_bytes() == b"export"
    assert library.get_asset("asset")["path"] == f"videos/{video_id}/source/asset.mkv"
    # An existing preview is kept until manually cleared, never silently rebuilt.
    with pytest.raises(ValueError):
        service.create(video_id, "asset")
    assert len(library.subtitle_cues(str(source["id"]))) == 1


def test_preview_failure_keeps_subtitles_exports_and_is_retryable(tmp_path: Path) -> None:
    library, video_id, encoder, service, source = setup_preview(tmp_path)
    encoder.fail = True
    job = service.create(video_id, "asset")
    service.run(job, library.start_job(str(job["id"])), Event())
    failed = library.get_job(str(job["id"]))
    assert failed["status"] == "failed" and failed["error_code"] == "preview_failed"
    assert library.previews(video_id) == []
    assert len(library.subtitle_cues(str(source["id"]))) == 1
    assert (library.video_dir(video_id) / "exports" / "done.mp4").read_bytes() == b"export"
    encoder.fail = False
    library.retry_job(str(job["id"]))
    service.run(job, library.start_job(str(job["id"])), Event())
    assert len(library.previews(video_id)) == 1


@pytest.mark.parametrize("action", ["cancel", "delete"])
def test_late_preview_is_rejected_by_common_gate(tmp_path: Path, action: str) -> None:
    library, video_id, encoder, service, _ = setup_preview(tmp_path)
    job = service.create(video_id, "asset")
    job_id = str(job["id"])

    def late() -> None:
        if action == "cancel":
            library.cancel_job(job_id)
        else:
            library.mark_deleting(video_id)

    encoder.on_encode = late
    service.run(job, library.start_job(job_id), Event())
    with library._connect() as connection:
        assert connection.execute("SELECT COUNT(*) FROM media_previews").fetchone()[0] == 0
    leftovers = [
        path.name
        for path in (library.root / "videos" / video_id / "previews").iterdir()
        if not path.name.startswith(".")
    ]
    assert leftovers == []
    assert library.get_job(job_id)["status"] == "cancelled"


def test_preview_space_preflight_blocks_encode(tmp_path: Path, monkeypatch) -> None:
    from collections import namedtuple

    from video_content_capture.workspace import previews

    library, video_id, encoder, service, _ = setup_preview(tmp_path)
    usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(previews.shutil, "disk_usage", lambda root: usage(100, 100, 0))
    job = service.create(video_id, "asset")
    service.run(job, library.start_job(str(job["id"])), Event())
    assert encoder.calls == 0
    assert library.get_job(str(job["id"]))["error_code"] == "insufficient_space"
