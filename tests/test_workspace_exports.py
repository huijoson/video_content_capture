import json
import subprocess
from pathlib import Path
from threading import Event

import pytest

from video_content_capture.workspace.exports import (
    ExportTrack,
    MediaExporter,
    export_filename,
    select_tracks,
)
from video_content_capture.workspace.youtube import SourceError


def track(identifier: str, language: str = "en", name: str = "Original") -> ExportTrack:
    return ExportTrack(
        version_id=identifier,
        language=language,
        name=name,
        srt="1\n00:00:00,000 --> 00:00:00,500\nHello\n",
    )


@pytest.mark.parametrize(
    ("target", "original", "include", "count"),
    [
        (track("target", "zh-TW"), track("original"), False, 1),
        (track("target", "zh-TW"), track("original"), True, 2),
        (track("same"), track("same"), True, 1),
        (track("revision", name="Revised"), track("original"), True, 2),
    ],
)
def test_export_matrix_real_mux(
    tmp_path: Path, target: ExportTrack, original: ExportTrack, include: bool, count: int
) -> None:
    source = tmp_path / "source.mp4"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=32x32:r=10",
            "-f",
            "lavfi",
            "-i",
            "anullsrc=r=8000:cl=mono",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-c:a",
            "aac",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    tracks = select_tracks([target], original, include)
    exporter = MediaExporter()
    container = exporter.preview(source, tracks, tmp_path / "preview", Event())
    assert container == "mp4"
    output = exporter.mux(source, tracks, container, tmp_path / "output", Event())
    info = json.loads(
        subprocess.check_output(
            ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(output)]
        )
    )
    subtitles = [s for s in info["streams"] if s["codec_type"] == "subtitle"]
    assert len(subtitles) == count
    assert [s["tags"]["language"] for s in subtitles] == [t.language_tag for t in tracks]
    assert [s["codec_name"] for s in info["streams"] if s["codec_type"] != "subtitle"] == [
        "h264",
        "aac",
    ]


def test_filename_preserves_unicode_without_paths() -> None:
    name = export_filename("../台灣\\影片\x00 / test", "abcdefghijk", "1080p", "a" * 32, "mp4")
    assert "台灣" in name
    assert "/" not in name and "\\" not in name and "\x00" not in name
    assert name.endswith("abcdefghijk-1080p-" + "a" * 32 + ".mp4")


def test_both_containers_fail_preserves_inputs(tmp_path: Path) -> None:
    source = tmp_path / "source.mp4"
    source.write_bytes(b"source")
    calls: list[list[str]] = []

    def fail(args: list[str], cancel: Event) -> str:
        calls.append(args)
        raise SourceError("source_unavailable", "fail")

    with pytest.raises(SourceError, match="兩種"):
        MediaExporter(fail).preview(source, [track("target")], tmp_path / "preview", Event())
    assert source.read_bytes() == b"source"
    assert len(calls) == 2


def setup_service(tmp_path: Path):
    from video_content_capture.workspace.exports import ExportService
    from video_content_capture.workspace.jobs import checksum
    from video_content_capture.workspace.storage import Library
    from video_content_capture.workspace.subtitles import Cue

    library = Library(tmp_path / "library")
    library.initialize()
    video = library.import_video("abcdefghijk", "台灣 / 影片", 1, "url", "{}")
    video_id = str(video["id"])
    library.video_dir(video_id)
    source = library.root / "videos" / video_id / "source" / "media.mp4"
    source.write_bytes(b"source")
    with library._connect() as db:
        db.execute(
            "INSERT INTO media_assets VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "asset",
                video_id,
                "1080p",
                "a",
                str(source.relative_to(library.root)),
                checksum(source),
                "fingerprint",
                1,
                "mp4",
            ),
        )
    cue = Cue(id="one", start=0, end=0.5, text="original")
    original = library.create_subtitle_version(video_id, "en", "original", "import", [cue])
    target = library.create_subtitle_version(video_id, "zh-TW", "target", "import", [cue])

    class FakeExporter(MediaExporter):
        def __init__(self):
            self.tracks = []
            self.on_mux = lambda: None

        def preview(self, source, tracks, directory, cancel):
            return "mp4"

        def mux(self, source, tracks, container, directory, cancel, *, preview=False):
            self.tracks = tracks
            self.on_mux()
            output = directory / "fake.mp4"
            output.write_bytes(b"verified export")
            return output

    exporter = FakeExporter()
    return library, video_id, original, target, exporter, ExportService(library, exporter)


def test_queued_export_reads_snapshot_and_publishes_unique_artifact(tmp_path: Path) -> None:
    library, video_id, original, target, exporter, service = setup_service(tmp_path)
    snapshot = service.preview(
        video_id, "asset", [str(target["id"])], True, str(original["id"]), str(original["id"])
    )
    job = service.create(video_id, snapshot)
    library.set_subtitle_selection(video_id, "export", str(original["id"]))
    attempt = library.start_job(str(job["id"]))
    service.run(job, attempt, Event())
    assert library.get_job(str(job["id"]))["status"] == "completed"
    assert [t.version_id for t in exporter.tracks] == [target["id"], original["id"]]
    artifact = library.exports(video_id)[0]
    assert json.loads(str(artifact["summary"])) == snapshot
    assert (library.root / str(artifact["path"])).read_bytes() == b"verified export"
    again = service.create(video_id, snapshot)
    service.run(again, library.start_job(str(again["id"])), Event())
    assert len(library.exports(video_id)) == 2
    assert len({a["path"] for a in library.exports(video_id)}) == 2


def test_cancel_retry_late_export_cannot_publish(tmp_path: Path) -> None:
    library, video_id, _, target, exporter, service = setup_service(tmp_path)
    snapshot = service.preview(video_id, "asset", [str(target["id"])])
    job = service.create(video_id, snapshot)
    job_id = str(job["id"])
    attempt = library.start_job(job_id)

    def cancel_retry():
        library.cancel_job(job_id)
        library.retry_job(job_id)
        library.start_job(job_id)

    exporter.on_mux = cancel_retry
    service.run(job, attempt, Event())
    assert library.exports(video_id) == []
    assert library.get_job(job_id)["status"] == "running"


def test_mp4_failure_selects_mkv_and_keeps_language_region(tmp_path: Path) -> None:
    from video_content_capture.workspace.youtube import run_process

    source = tmp_path / "source.mkv"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=s=32x32:r=10",
            "-t",
            "0.5",
            "-c:v",
            "ffv1",
            str(source),
        ],
        check=True,
        capture_output=True,
    )
    selected = [track("tw", "zh-TW"), track("cn", "zh-CN")]
    calls = []

    def runner(args, cancel):
        calls.append(args)
        return run_process(args, cancel)

    exporter = MediaExporter(runner)
    assert exporter.preview(source, selected, tmp_path / "preview", Event()) == "mkv"
    assert any("-c:v" in args and args[-1].endswith(".mp4") for args in calls)
    assert selected[0].title != selected[1].title


def test_export_preview_reconfirms_mkv_in_new_snapshot(tmp_path: Path) -> None:
    library, video_id, _, target, exporter, service = setup_service(tmp_path)
    first = service.preview(video_id, "asset", [str(target["id"])])
    confirmed = service.preview(video_id, "asset", [str(target["id"])], container="mkv")
    assert first["container"] == "mp4"
    assert confirmed["container"] == "mkv"
    job = service.create(video_id, confirmed)
    assert json.loads(str(job["snapshot"]))["container"] == "mkv"


def test_export_snapshot_tampering_is_rejected(tmp_path: Path) -> None:
    library, video_id, _, target, exporter, service = setup_service(tmp_path)
    snapshot = service.preview(video_id, "asset", [str(target["id"])])
    snapshot["target_languages"] = ["ja"]
    with pytest.raises(ValueError, match="摘要已變更"):
        service.create(video_id, snapshot)
    assert library.list_jobs() == []


def test_runtime_failure_preserves_source_subtitles_and_requires_confirmation(
    tmp_path: Path,
) -> None:
    library, video_id, _, target, exporter, service = setup_service(tmp_path)
    snapshot = service.preview(video_id, "asset", [str(target["id"])])
    job = service.create(video_id, snapshot)
    calls = []

    def fail_mp4(source, tracks, container, directory, cancel, *, preview=False):
        calls.append(container)
        if container == "mp4":
            raise SourceError("source_unavailable", "mux failed")
        return directory / "probe.mkv"

    exporter.mux = fail_mp4
    service.run(job, library.start_job(str(job["id"])), Event())
    assert calls == ["mp4", "mkv"]
    assert library.get_job(str(job["id"]))["error_code"] == "container_confirmation_required"
    assert library.exports(video_id) == []
    assert len(library.subtitle_cues(str(target["id"]))) == 1
    assert (library.root / str(library.get_asset("asset")["path"])).read_bytes() == b"source"


def test_unknown_two_letter_language_fails_instead_of_silent_und() -> None:
    assert track("portuguese", "pt-BR").language_tag == "por"
    assert track("arabic", "ar").language_tag == "ara"
    assert track("cantonese", "yue").language_tag == "yue"
    with pytest.raises(SourceError, match="語言"):
        _ = track("unknown", "xx").language_tag


def test_unicode_filename_fits_filesystem_byte_limit() -> None:
    name = export_filename("台灣" * 200, "abcdefghijk", "1080p", "a" * 32, "mp4")
    assert len(name.encode("utf-8")) < 255
    assert "台灣" in name


def test_export_space_preflight_blocks_mux_and_preserves_inputs(
    tmp_path: Path, monkeypatch
) -> None:
    from collections import namedtuple

    from video_content_capture.workspace import exports

    library, video_id, _, target, exporter, service = setup_service(tmp_path)
    snapshot = service.preview(video_id, "asset", [str(target["id"])])
    job = service.create(video_id, snapshot)
    calls = []
    exporter.on_mux = lambda: calls.append("mux")
    usage = namedtuple("Usage", "total used free")
    monkeypatch.setattr(exports.shutil, "disk_usage", lambda root: usage(100, 100, 0))
    service.run(job, library.start_job(str(job["id"])), Event())
    assert calls == []
    assert library.get_job(str(job["id"]))["error_code"] == "insufficient_space"
    assert library.exports(video_id) == []
    assert len(library.subtitle_cues(str(target["id"]))) == 1
    assert (library.root / str(library.get_asset("asset")["path"])).read_bytes() == b"source"


@pytest.mark.parametrize("count", [0, 2])
def test_export_requires_exactly_one_target_version(tmp_path: Path, count: int) -> None:
    library, video_id, _, target, exporter, service = setup_service(tmp_path)
    with pytest.raises(ValueError, match="一份"):
        service.preview(video_id, "asset", [str(target["id"])] * count)
    assert library.list_jobs() == []
