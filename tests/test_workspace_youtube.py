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


_HELPER_STUB = """\
import os


class DownloadError(Exception):
    def __init__(self, message, exc_info=None):
        super().__init__(message)
        self.exc_info = exc_info


class Response:
    def __init__(self, status):
        self.status = status


class YoutubeDL:
    def __init__(self, options):
        self.options = options

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def extract_info(self, url, download=False):
        {statement}
"""

_HELPER_EXCEPTIONS_STUB = """\
class HTTPError(Exception):
    def __init__(self, response=None, msg=None):
        super().__init__(msg or "http error")
        self.status = getattr(response, "status", None)


class TransportError(Exception):
    pass
"""

_HELPER_SCENARIOS = {
    "bot_check": (
        'raise DownloadError("ERROR: [youtube] Abc_123-xyz: '
        "Sign in to confirm you're not a bot. cookie-secret-sentinel\")"
    ),
    "http_429": (
        "from yt_dlp.networking.exceptions import HTTPError\n"
        '        raise DownloadError("ERROR: HTTP Error 429: Too Many Requests", '
        "exc_info=(HTTPError, HTTPError(Response(429)), None))"
    ),
    "transport": (
        "from yt_dlp.networking.exceptions import TransportError\n"
        '        raise DownloadError("ERROR: unable to download video data: Connection reset", '
        "exc_info=(TransportError, TransportError('reset'), None))"
    ),
    "format": 'raise DownloadError("ERROR: Requested format is not available")',
    "gone": (
        'raise DownloadError("ERROR: [youtube] Abc_123-xyz: '
        'Private video. Sign in if granted access")'
    ),
    "mystery": 'raise DownloadError("ERROR: [youtube] Abc_123-xyz: a failure nobody has seen yet")',
}


@pytest.mark.parametrize(
    ("scenario", "reason"),
    [
        ("bot_check", "blocked"),
        ("http_429", "temporary"),
        ("transport", "temporary"),
        ("format", "format_unavailable"),
        ("gone", "unavailable"),
        ("mystery", "unavailable"),
    ],
)
def test_helper_reports_a_redacted_reason_instead_of_failing_silently(
    tmp_path: Path, scenario: str, reason: str
) -> None:
    """stderr is discarded, so the failing helper must name the reason without echoing it."""
    import json
    import os
    import subprocess
    import sys

    from video_content_capture.workspace import youtube

    package = tmp_path / "yt_dlp"
    (package / "networking").mkdir(parents=True)
    (package / "networking" / "__init__.py").write_text("")
    (package / "networking" / "exceptions.py").write_text(_HELPER_EXCEPTIONS_STUB)
    (package / "__init__.py").write_text(_HELPER_STUB.format(statement=_HELPER_SCENARIOS[scenario]))
    completed = subprocess.run(
        [sys.executable, "-c", youtube._HELPER],
        input=json.dumps({"mode": "query", "url": f"https://www.youtube.com/watch?v={ID}"}),
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONPATH": str(tmp_path)},
        cwd=tmp_path,
        timeout=60,
    )
    assert completed.returncode == 0, completed.stderr
    assert json.loads(completed.stdout) == {"error": reason}
    assert "cookie-secret-sentinel" not in completed.stdout + completed.stderr


def _payload(monkeypatch: pytest.MonkeyPatch, answers: list[str]) -> list[dict[str, object]]:
    """Answer the helper with `answers` in order; a SourceError stops the script."""
    import json

    from video_content_capture.workspace import youtube

    requests: list[dict[str, object]] = []

    def fake_process(command: list[str], cancel: Event, **kwargs: object) -> str:
        requests.append(json.loads(str(kwargs["request"])))
        return answers[min(len(requests), len(answers)) - 1]

    monkeypatch.setattr(youtube, "run_process", fake_process)
    monkeypatch.setattr(youtube, "_TRANSIENT_DELAYS", (0.0, 0.0))
    return requests


def test_transient_helper_failure_is_retried_before_it_reaches_the_stage(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = YtDlpAdapter()
    requests = _payload(monkeypatch, ['{"error": "temporary"}'] * 3)
    with pytest.raises(SourceError) as error:
        adapter.query(f"https://youtu.be/{ID}")
    assert error.value.code == "source_busy"
    assert len(requests) == 3


def test_transient_helper_failure_recovers_on_the_next_attempt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import json

    adapter = YtDlpAdapter()
    requests = _payload(monkeypatch, ['{"error": "temporary"}', json.dumps(info())])
    source = adapter.query(f"https://youtu.be/{ID}")
    assert source.title == "Example"
    assert len(requests) == 2


@pytest.mark.parametrize(
    ("reason", "code"),
    [
        ("blocked", "source_blocked"),
        ("format_unavailable", "format_unavailable"),
        ("unavailable", "source_unavailable"),
    ],
)
def test_permanent_helper_failures_are_not_retried(
    monkeypatch: pytest.MonkeyPatch, reason: str, code: str
) -> None:
    import json

    adapter = YtDlpAdapter()
    requests = _payload(monkeypatch, [json.dumps({"error": reason})] * 3)
    with pytest.raises(SourceError) as error:
        adapter.query(f"https://youtu.be/{ID}")
    assert error.value.code == code
    assert len(requests) == 1


def test_cancelling_during_the_backoff_ends_the_stage_immediately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    adapter = YtDlpAdapter()
    requests = _payload(monkeypatch, ['{"error": "temporary"}'] * 3)
    cancel = Event()
    cancel.set()
    with pytest.raises(SourceError) as error:
        adapter.query(f"https://youtu.be/{ID}", cancel)
    assert error.value.code == "cancelled"
    assert len(requests) == 1
