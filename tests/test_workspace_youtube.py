from pathlib import Path
from threading import Event

import pytest

from video_content_capture.workspace.youtube import (
    SourceError,
    YtDlpAdapter,
    parse_metadata,
    parse_youtube_url,
)

ID = "Abc_123-xyz"


@pytest.mark.parametrize(
    "url",
    [
        f"https://www.youtube.com/watch?v={ID}&list=PL123",
        f"https://youtu.be/{ID}?t=30",
        f"https://m.youtube.com/shorts/{ID}",
        f"https://music.youtube.com/watch?v={ID}",
        f"https://youtube.com/embed/{ID}",
    ],
)
def test_single_video_urls(url: str) -> None:
    assert parse_youtube_url(url) == (ID, f"https://www.youtube.com/watch?v={ID}")


@pytest.mark.parametrize(
    "url",
    [
        "file:///tmp/movie.mp4",
        "/tmp/movie.mp4",
        "https://example.com/watch?v=Abc_123-xyz",
        "https://youtube.com/playlist?list=PL123",
        "https://youtube.com/watch?v=short",
        "https://youtube.com.evil.test/watch?v=Abc_123-xyz",
        "https://user:secret@youtube.com/watch?v=Abc_123-xyz",
        "https://youtu.be/Abc_123-xyz https://youtu.be/Abc_123-xyz",
        "https://youtube.com/watch?v=Abc_123-xyz&v=ZZZ_123-xyz",
    ],
)
def test_invalid_urls(url: str) -> None:
    with pytest.raises(SourceError, match="YouTube"):
        parse_youtube_url(url)


def info() -> dict[str, object]:
    return {
        "id": ID,
        "title": "Example",
        "duration": 30,
        "formats": [
            {"format_id": "a", "vcodec": "none", "acodec": "mp4a.40.2"},
            {"format_id": "v720", "height": 720, "fps": 30, "vcodec": "avc1.64001f"},
            {"format_id": "v1080", "height": 1080, "fps": 30, "vcodec": "avc1.640028"},
            {"format_id": "v1080f", "height": 1080, "fps": 60, "vcodec": "vp9"},
            {"format_id": "v4k", "height": 2160, "fps": 60, "vcodec": "av01"},
        ],
    }


def test_quality_and_unknown_audio() -> None:
    result = parse_metadata(info(), f"https://youtu.be/{ID}")
    assert result.default_format_id == "v1080f"
    assert not result.above_1080p
    assert result.original_language is None
    assert result.audio_tracks[0].language == "未知"
    assert not result.audio_tracks[0].original


def test_all_above_1080_and_downloader_tie_order() -> None:
    data = info()
    data["formats"] = [
        {"format_id": "a", "vcodec": "none", "acodec": "opus"},
        {"format_id": "first", "height": 1440, "fps": 30, "vcodec": "vp9"},
        {"format_id": "last", "height": 1440, "fps": 30, "vcodec": "vp9"},
        {"format_id": "4k", "height": 2160, "fps": 60, "vcodec": "vp9"},
    ]
    result = parse_metadata(data, f"https://youtu.be/{ID}")
    assert result.default_format_id == "last"
    assert result.above_1080p


def test_original_default_track_does_not_choose_dub() -> None:
    data = info()
    data["language"] = "ja"
    formats = data["formats"]
    assert isinstance(formats, list)
    formats.extend(
        [
            {
                "format_id": "dub",
                "vcodec": "none",
                "acodec": "opus",
                "language": "en",
                "format_note": "English (dubbed)",
                "language_preference": 10,
            },
            {
                "format_id": "original",
                "vcodec": "none",
                "acodec": "opus",
                "language": "ja",
                "format_note": "Japanese (original)",
                "language_preference": 10,
            },
        ]
    )
    result = parse_metadata(data, f"https://youtu.be/{ID}")
    assert result.default_audio_id == "original"
    assert not next(track for track in result.audio_tracks if track.id == "dub").original


@pytest.mark.parametrize(
    "update", [{"is_live": True}, {"availability": "needs_auth"}, {"formats": []}]
)
def test_unsupported_sources(update: dict[str, object]) -> None:
    data = info()
    data.update(update)
    with pytest.raises(SourceError):
        parse_metadata(data, f"https://youtu.be/{ID}")


def test_formats_disappearing_fail_without_download(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    adapter = YtDlpAdapter()
    source = parse_metadata(info(), f"https://youtu.be/{ID}")
    monkeypatch.setattr(adapter, "query", lambda url, cancel=None: source)
    with pytest.raises(SourceError) as error:
        adapter.download(source, "missing", "a", tmp_path, Event(), lambda *args: None)
    assert error.value.code == "format_unavailable"


def test_subtitle_inventory_never_keeps_signed_urls() -> None:
    data = info()
    data["subtitles"] = {"ja": [{"ext": "vtt", "url": "https://example.test/?token=secret"}]}
    source = parse_metadata(data, f"https://youtu.be/{ID}")
    assert source.subtitles[0].extensions == ["vtt"]
    assert "secret" not in source.model_dump_json()


def test_cancelled_process_terminates_group(monkeypatch: pytest.MonkeyPatch) -> None:
    import subprocess

    from video_content_capture.workspace import youtube

    cancelled = Event()
    terminated: list[int] = []

    class FakeProcess:
        pid = 12345
        returncode: int | None = None

        def communicate(self, *, input=None, timeout=None):
            cancelled.set()
            raise subprocess.TimeoutExpired("fake", timeout)

        def poll(self):
            return self.returncode

        def wait(self, *, timeout=None):
            self.returncode = -15
            return self.returncode

    monkeypatch.setattr(youtube.subprocess, "Popen", lambda *args, **kwargs: FakeProcess())
    monkeypatch.setattr(youtube.os, "killpg", lambda pid, sig: terminated.append(pid))
    with pytest.raises(SourceError) as error:
        youtube.run_process(["fake"], cancelled)
    assert error.value.code == "cancelled"
    assert terminated == [12345]


def test_language_alone_does_not_claim_original() -> None:
    data = info()
    data["language"] = "en"
    data["formats"] = [
        {"format_id": "v", "height": 720, "vcodec": "h264"},
        {"format_id": "a", "vcodec": "none", "acodec": "aac", "language": "en"},
    ]
    source = parse_metadata(data, f"https://youtu.be/{ID}")
    assert not source.audio_tracks[0].original


def test_local_stream_copy_and_probe(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import shutil
    import subprocess

    if not shutil.which("ffmpeg") or not shutil.which("ffprobe"):
        pytest.skip("ffmpeg and ffprobe are required for the local synthetic integration")
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.m4a"
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "color=c=black:s=64x64:r=25",
            "-t",
            "1",
            "-c:v",
            "libx264",
            "-pix_fmt",
            "yuv420p",
            str(video),
        ],
        check=True,
        capture_output=True,
    )
    subprocess.run(
        [
            "ffmpeg",
            "-nostdin",
            "-v",
            "error",
            "-f",
            "lavfi",
            "-i",
            "sine=frequency=440:sample_rate=44100",
            "-t",
            "1",
            "-c:a",
            "aac",
            str(audio),
        ],
        check=True,
        capture_output=True,
    )
    data = info()
    data["duration"] = 1
    data["formats"] = [
        {"format_id": "v", "height": 64, "fps": 25, "vcodec": "avc1.64001f"},
        {"format_id": "a", "vcodec": "none", "acodec": "mp4a.40.2"},
    ]
    source = parse_metadata(data, f"https://youtu.be/{ID}")
    adapter = YtDlpAdapter()
    monkeypatch.setattr(adapter, "query", lambda url, cancel=None: source)
    monkeypatch.setattr(
        adapter,
        "_api",
        lambda request, cancel: {
            "path": str(video if request["format"] == "v" else audio),
        },
    )
    stages: list[str] = []
    result = adapter.download(
        source, "v", "a", tmp_path, Event(), lambda stage, done, total: stages.append(stage)
    )
    assert result.path.is_file()
    assert result.browser_playable
    assert result.video_codec == "h264"
    assert result.audio_codec == "aac"
    assert stages == ["downloading_video", "downloading_audio", "merging", "verifying"]


@pytest.mark.parametrize(
    "change",
    [
        {"duration": "nan"},
        {"duration": "0"},
        {"height": 1},
        {"video_codec": "vp9"},
    ],
)
def test_probe_rejects_invalid_selected_media(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    change: dict[str, object],
) -> None:
    import json

    from video_content_capture.workspace import youtube

    data = info()
    data["formats"] = [
        {"format_id": "v", "height": 64, "vcodec": "h264"},
        {"format_id": "a", "vcodec": "none", "acodec": "aac"},
    ]
    source = parse_metadata(data, f"https://youtu.be/{ID}")
    video = tmp_path / "video.mp4"
    audio = tmp_path / "audio.m4a"
    video.write_bytes(b"fake")
    audio.write_bytes(b"fake")
    adapter = YtDlpAdapter()
    monkeypatch.setattr(adapter, "query", lambda url, cancel=None: source)
    monkeypatch.setattr(
        adapter,
        "_api",
        lambda request, cancel: {
            "path": str(video if request["format"] == "v" else audio),
        },
    )

    def fake_process(command, cancel, **kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"unverified")
            return ""
        return json.dumps(
            {
                "streams": [
                    {
                        "codec_type": "video",
                        "codec_name": change.get("video_codec", "h264"),
                        "height": change.get("height", 64),
                    },
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
                "format": {"duration": change.get("duration", "30")},
            }
        )

    monkeypatch.setattr(youtube, "run_process", fake_process)
    with pytest.raises(SourceError) as error:
        adapter.download(source, "v", "a", tmp_path, Event(), lambda *args: None)
    assert error.value.code == "invalid_media"


def test_completed_video_stage_survives_audio_failure_and_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    from video_content_capture.workspace import youtube

    data = info()
    data["formats"] = [
        {"format_id": "v", "height": 64, "vcodec": "h264"},
        {"format_id": "a", "vcodec": "none", "acodec": "aac"},
    ]
    source = parse_metadata(data, f"https://youtu.be/{ID}")
    saved: dict[tuple[str, str], Path] = {}
    downloads: list[str] = []

    class Stages:
        def load(self, name: str, format_id: str) -> Path | None:
            return saved.get((name, format_id))

        def save(self, name: str, format_id: str, path: Path) -> None:
            saved[name, format_id] = path

    adapter = YtDlpAdapter()
    monkeypatch.setattr(adapter, "query", lambda url, cancel=None: source)

    def fake_api(request, cancel):
        identifier = request["format"]
        downloads.append(identifier)
        if identifier == "a" and downloads.count("a") == 1:
            raise SourceError("source_unavailable", "offline failure")
        path = tmp_path / f"{identifier}.mp4"
        path.write_bytes(b"verified stage fixture")
        return {"path": str(path)}

    def fake_process(command, cancel, **kwargs):
        if command[0] == "ffmpeg":
            Path(command[-1]).write_bytes(b"verified merged fixture")
            return ""
        return json.dumps(
            {
                "streams": [
                    {"codec_type": "video", "codec_name": "h264", "height": 64},
                    {"codec_type": "audio", "codec_name": "aac"},
                ],
                "format": {"duration": "30"},
            }
        )

    monkeypatch.setattr(adapter, "_api", fake_api)
    monkeypatch.setattr(youtube, "run_process", fake_process)
    stages = Stages()
    with pytest.raises(SourceError, match="offline failure"):
        adapter.download(source, "v", "a", tmp_path, Event(), lambda *args: None, stages)
    assert ("video", "v") in saved
    result = adapter.download(source, "v", "a", tmp_path, Event(), lambda *args: None, stages)
    assert downloads == ["v", "a", "a"]
    assert result.path.is_file()
