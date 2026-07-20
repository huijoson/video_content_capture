"""Focused media tests for OpenSpec group 4.

Covers:
* ffprobe JSON parsing into provider-neutral ``MediaMetadata``.
* Missing file raises a typed ``MediaError``.
* Media without an audio stream raises a typed ``MediaError``.
* Chinese paths with spaces are passed to subprocess as argument-array
  elements without shell interpolation.
* AAC stream-copy extraction arguments map only the selected audio stream,
  never decode video frames, and write a ``.m4a`` artifact.
* Post-extraction validation rejects a missing/non-audio artifact.
* The source MP4 path is never configured as the transcription upload
  artifact — it is only ever an extraction input.

Subprocess execution is mocked so no real ffmpeg/ffprobe process runs and no
paid cloud work is performed.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from video_content_capture.domain.errors import MediaError
from video_content_capture.domain.models import MediaMetadata

# --- Fixtures ---------------------------------------------------------------

# ffprobe JSON for the target Mandarin financial-program MP4. The real file is
# ~2060.17s (34:20), one HEVC video stream, one AAC audio stream, no subtitles.
REAL_FFPROBE_JSON: dict[str, Any] = {
    "streams": [
        {
            "index": 0,
            "codec_name": "hevc",
            "codec_type": "video",
            "width": 1180,
            "height": 2556,
            "duration": "2060.141667",
            "tags": {"language": "und"},
        },
        {
            "index": 1,
            "codec_name": "aac",
            "codec_type": "audio",
            "sample_rate": "44100",
            "channels": 2,
            "duration": "2060.141769",
            "tags": {"language": "und"},
        },
    ],
    "format": {
        "filename": "視野環球財經robots_07-19-2026 22-11-19_1.MP4",
        "nb_streams": 2,
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "format_long_name": "QuickTime / MOV",
        "duration": "2060.167143",
        "size": "1545677326",
        "bit_rate": "6002143",
    },
}


def _probe_payload() -> str:
    return json.dumps(REAL_FFPROBE_JSON)


# A media file with video but no audio stream.
NO_AUDIO_FFPROBE_JSON: dict[str, Any] = {
    "streams": [
        {
            "index": 0,
            "codec_name": "hevc",
            "codec_type": "video",
            "width": 1180,
            "height": 2556,
            "duration": "10.000000",
        }
    ],
    "format": {
        "filename": "noaudio.mp4",
        "nb_streams": 1,
        "format_name": "mov,mp4,m4a,3gp,3g2,mj2",
        "duration": "10.000000",
    },
}


@pytest.fixture
def video_file(tmp_path: Path) -> Path:
    """A fake but readable video file path."""

    p = tmp_path / "視野環球財經 robots 07-19-2026.MP4"
    p.write_bytes(b"\x00\x00\x00\x18ftypmp42")  # minimal mp4 header stub
    return p


# --- Probe: ffprobe JSON parsing -------------------------------------------


def test_probe_parses_ffprobe_json_into_media_metadata(mocker: Any, video_file: Path) -> None:
    """Probe parses ffprobe JSON into provider-neutral MediaMetadata."""

    from video_content_capture.media import probe as probe_mod

    mock_run = mocker.patch.object(probe_mod.subprocess, "run")
    mock_run.return_value = mocker.Mock(stdout=_probe_payload(), returncode=0)

    metadata = probe_mod.probe_video(video_file)

    assert isinstance(metadata, MediaMetadata)
    assert metadata.source_path == str(video_file)
    assert metadata.container == "mov,mp4,m4a,3gp,3g2,mj2"
    # ~2060.17 seconds ≈ 34:20.
    assert 2059.0 < metadata.duration_seconds < 2061.0
    assert len(metadata.video_streams) == 1
    assert metadata.video_streams[0].codec == "hevc"
    assert len(metadata.audio_streams) == 1
    assert metadata.audio_streams[0].codec == "aac"
    assert metadata.audio_streams[0].channels == 2
    assert metadata.audio_streams[0].sample_rate == 44100
    assert metadata.subtitle_streams == []


def test_probe_uses_argument_array_with_shell_false(mocker: Any, video_file: Path) -> None:
    """ffprobe must be invoked with an argument array and shell=False/default."""

    from video_content_capture.media import probe as probe_mod

    mock_run = mocker.patch.object(probe_mod.subprocess, "run")
    mock_run.return_value = mocker.Mock(stdout=_probe_payload(), returncode=0)

    probe_mod.probe_video(video_file)

    args, kwargs = mock_run.call_args
    argv = list(args[0])
    # argv must be a list of arguments, no shell interpolation.
    assert isinstance(argv, list)
    assert argv[0] == "ffprobe"
    assert "-print_format" in argv and "json" in argv
    assert "-show_format" in argv
    assert "-show_streams" in argv
    # The Chinese-and-space path must be a single argument element, preserved.
    assert str(video_file) in argv
    # shell must not be True.
    assert not kwargs.get("shell", False)


def test_probe_chinese_path_with_spaces_preserved_in_argv(mocker: Any, tmp_path: Path) -> None:
    """Chinese characters and spaces must be preserved as one argv element."""

    from video_content_capture.media import probe as probe_mod

    chinese_path = tmp_path / "視野環球財經 robots 07-19-2026.MP4"
    chinese_path.write_bytes(b"\x00")

    mock_run = mocker.patch.object(probe_mod.subprocess, "run")
    mock_run.return_value = mocker.Mock(stdout=_probe_payload(), returncode=0)

    probe_mod.probe_video(chinese_path)

    argv = list(mock_run.call_args[0][0])
    # The path appears exactly once as a single argv element, never split.
    assert chinese_path == Path(argv[-1])
    # And no shell-quoted form (quotes, escapes) leaked into argv.
    assert all("\\ " not in a and "'" not in a for a in argv)


def test_probe_raises_media_error_for_missing_file(tmp_path: Path) -> None:
    """Probing a path that does not exist must raise a typed MediaError."""

    from video_content_capture.media.probe import probe_video

    missing = tmp_path / "does-not-exist.mp4"
    with pytest.raises(MediaError, match="(?i)not found|missing|exist"):
        probe_video(missing)


def test_probe_raises_media_error_when_ffprobe_fails(mocker: Any, video_file: Path) -> None:
    """ffprobe nonzero exit must surface as a MediaError, not a raw exception."""

    from video_content_capture.media import probe as probe_mod

    mock_run = mocker.patch.object(probe_mod.subprocess, "run")
    mock_run.return_value = mocker.Mock(stdout="{}", returncode=1)

    with pytest.raises(MediaError):
        probe_mod.probe_video(video_file)


def test_probe_raises_media_error_for_no_audio_stream(mocker: Any, video_file: Path) -> None:
    """A readable media file with no audio stream must raise a MediaError."""

    from video_content_capture.media import probe as probe_mod

    mock_run = mocker.patch.object(probe_mod.subprocess, "run")
    mock_run.return_value = mocker.Mock(stdout=json.dumps(NO_AUDIO_FFPROBE_JSON), returncode=0)

    with pytest.raises(MediaError, match="(?i)audio"):
        probe_mod.probe_video(video_file)


def test_probe_raises_media_error_for_malformed_ffprobe_json(mocker: Any, video_file: Path) -> None:
    """Malformed ffprobe output must raise a MediaError, not crash silently."""

    from video_content_capture.media import probe as probe_mod

    mock_run = mocker.patch.object(probe_mod.subprocess, "run")
    mock_run.return_value = mocker.Mock(stdout="not-json{", returncode=0)

    with pytest.raises(MediaError):
        probe_mod.probe_video(video_file)


# --- Audio extraction ------------------------------------------------------


def test_extract_audio_creates_output_parent_before_ffmpeg(
    mocker: Any,
    video_file: Path,
    tmp_path: Path,
) -> None:
    """A new --output-dir must exist before ffmpeg opens its output file."""

    from video_content_capture.media import audio as audio_mod

    metadata = MediaMetadata(
        source_path=str(video_file),
        container="mov,mp4,m4a,3gp,3g2,mj2",
        duration_seconds=10.0,
        audio_streams=[_audio_stream(index=1, codec="aac")],
    )
    out_path = tmp_path / "new-output" / "out.m4a"

    def fake_run(*_args: Any, **_kwargs: Any) -> Any:
        assert out_path.parent.is_dir()
        out_path.write_bytes(b"local-audio")
        return mocker.Mock(returncode=0, stdout="", stderr="")

    mocker.patch.object(audio_mod.subprocess, "run", side_effect=fake_run)
    mocker.patch(
        "video_content_capture.media.audio.probe_video",
        return_value=MediaMetadata(
            source_path=str(out_path),
            container="m4a",
            duration_seconds=10.0,
            audio_streams=[_audio_stream(index=0, codec="aac")],
        ),
    )

    assert audio_mod.extract_audio(metadata, out_path) == out_path


def test_extract_audio_uses_aac_stream_copy_to_m4a(
    mocker: Any, video_file: Path, tmp_path: Path
) -> None:
    """For AAC input, extraction must prefer stream-copy into .m4a."""

    from video_content_capture.media import audio as audio_mod

    metadata = MediaMetadata(
        source_path=str(video_file),
        container="mov,mp4,m4a,3gp,3g2,mj2",
        duration_seconds=2060.17,
        video_streams=[],
        audio_streams=[
            _audio_stream(index=1, codec="aac"),
        ],
        subtitle_streams=[],
    )
    out_path = tmp_path / "out.m4a"

    mock_run = mocker.patch.object(audio_mod.subprocess, "run")
    mock_run.return_value = mocker.Mock(returncode=0, stdout="", stderr="")

    # Create the artifact file so is_file()/stat() return real values without
    # globally patching Path (which would break pytest internals).
    out_path.write_bytes(b"\x00\x00\x00\x18ftypM4A ")
    # Probe the artifact after extraction: stub to a minimal audio metadata.
    mocker.patch(
        "video_content_capture.media.audio.probe_video",
        return_value=MediaMetadata(
            source_path=str(out_path),
            container="mov,mp4,m4a,3gp,3g2,mj2",
            duration_seconds=2060.17,
            video_streams=[],
            audio_streams=[_audio_stream(index=0, codec="aac")],
            subtitle_streams=[],
        ),
    )

    result_path = audio_mod.extract_audio(metadata, out_path)

    argv = list(mock_run.call_args[0][0])
    assert argv[0] == "ffmpeg"
    assert "-vn" in argv  # no video decoding/output
    assert "-map" in argv
    # The selected audio stream's ABSOLUTE index must be mapped. The fixture
    # audio stream has absolute index 1, so the map token is 0:1 (input 0,
    # stream 1). The audio-relative form 0:a:1 would select a non-existent
    # second audio stream on a file with only one audio track.
    assert argv[argv.index("-map") + 1] == "0:1"
    assert "0:a:" not in argv  # never use audio-relative indexing
    # Stream-copy codec (no transcoding) and m4a target.
    assert "-c:a" in argv or "-c" in argv
    codec_idx = argv.index("-c:a") if "-c:a" in argv else argv.index("-c")
    assert argv[codec_idx + 1] == "copy"
    assert str(out_path) in argv
    # And the artifact written is m4a.
    assert result_path.suffix == ".m4a"


def test_extract_audio_falls_back_to_transcode_when_codec_unsupported(
    mocker: Any, video_file: Path, tmp_path: Path
) -> None:
    """Non-AAC input must trigger a controlled audio-only transcode."""

    from video_content_capture.media import audio as audio_mod

    metadata = MediaMetadata(
        source_path=str(video_file),
        container="matroska",
        duration_seconds=10.0,
        video_streams=[],
        audio_streams=[_audio_stream(index=1, codec="opus")],
        subtitle_streams=[],
    )
    out_path = tmp_path / "out.m4a"

    mock_run = mocker.patch.object(audio_mod.subprocess, "run")
    mock_run.return_value = mocker.Mock(returncode=0, stdout="", stderr="")

    out_path.write_bytes(b"\x00\x00\x00\x18ftypM4A ")
    mocker.patch(
        "video_content_capture.media.audio.probe_video",
        return_value=MediaMetadata(
            source_path=str(out_path),
            container="mov,mp4,m4a,3gp,3g2,mj2",
            duration_seconds=10.0,
            video_streams=[],
            audio_streams=[_audio_stream(index=0, codec="aac")],
            subtitle_streams=[],
        ),
    )

    audio_mod.extract_audio(metadata, out_path)

    argv = list(mock_run.call_args[0][0])
    assert argv[0] == "ffmpeg"
    assert "-vn" in argv
    assert "-map" in argv
    # Absolute stream index for the fixture's opus audio (index 1).
    assert argv[argv.index("-map") + 1] == "0:1"
    # Codec must NOT be "copy" for unsupported source codec.
    codec_idx = argv.index("-c:a") if "-c:a" in argv else argv.index("-c")
    assert argv[codec_idx + 1] != "copy"
    # AAC target codec for the .m4a container.
    assert argv[codec_idx + 1] == "aac"


def test_extract_audio_maps_absolute_stream_index_for_target_layout(
    mocker: Any, video_file: Path, tmp_path: Path
) -> None:
    """For the real target file layout (video index 0, sole audio absolute
    index 1), ffmpeg must receive ``-map 0:1`` (absolute stream index), NOT
    ``0:a:1`` (audio-relative — would select a non-existent second audio
    stream and fail on the real file)."""

    from video_content_capture.media import audio as audio_mod

    metadata = MediaMetadata(
        source_path=str(video_file),
        container="mov,mp4,m4a,3gp,3g2,mj2",
        duration_seconds=2060.17,
        video_streams=[_video_stream(index=0, codec="hevc")],
        audio_streams=[_audio_stream(index=1, codec="aac")],
        subtitle_streams=[],
    )
    out_path = tmp_path / "out.m4a"

    mock_run = mocker.patch.object(audio_mod.subprocess, "run")
    mock_run.return_value = mocker.Mock(returncode=0, stdout="", stderr="")
    out_path.write_bytes(b"\x00\x00\x00\x18ftypM4A ")
    mocker.patch(
        "video_content_capture.media.audio.probe_video",
        return_value=MediaMetadata(
            source_path=str(out_path),
            container="mov,mp4,m4a,3gp,3g2,mj2",
            duration_seconds=2060.17,
            video_streams=[],
            audio_streams=[_audio_stream(index=0, codec="aac")],
            subtitle_streams=[],
        ),
    )

    audio_mod.extract_audio(metadata, out_path)

    argv = list(mock_run.call_args[0][0])
    # Exact absolute-index map token for the real file's audio stream.
    assert argv[argv.index("-map") + 1] == "0:1"
    # And the audio-relative form must NOT be used (it would select the wrong
    # stream on the real file).
    assert "0:a:1" not in argv


def test_extract_audio_maps_selected_stream_when_multiple_audio_streams(
    mocker: Any, video_file: Path, tmp_path: Path
) -> None:
    """When metadata lists multiple audio streams with non-contiguous absolute
    indices, extraction must map the absolute index of the *selected* stream
    (the first audio stream), not an audio-relative index."""

    from video_content_capture.media import audio as audio_mod

    metadata = MediaMetadata(
        source_path=str(video_file),
        container="matroska",
        duration_seconds=10.0,
        video_streams=[_video_stream(index=0, codec="h264")],
        audio_streams=[
            _audio_stream(index=1, codec="aac"),
            _audio_stream(index=3, codec="aac"),
        ],
        subtitle_streams=[],
    )
    out_path = tmp_path / "out.m4a"

    mock_run = mocker.patch.object(audio_mod.subprocess, "run")
    mock_run.return_value = mocker.Mock(returncode=0, stdout="", stderr="")
    out_path.write_bytes(b"\x00\x00\x00\x18ftypM4A ")
    mocker.patch(
        "video_content_capture.media.audio.probe_video",
        return_value=MediaMetadata(
            source_path=str(out_path),
            container="mov,mp4,m4a,3gp,3g2,mj2",
            duration_seconds=10.0,
            video_streams=[],
            audio_streams=[_audio_stream(index=0, codec="aac")],
            subtitle_streams=[],
        ),
    )

    audio_mod.extract_audio(metadata, out_path)

    argv = list(mock_run.call_args[0][0])
    # Selected stream is the first audio stream (absolute index 1), not
    # audio-relative 0:a:0 and not the second audio stream (absolute 3).
    assert argv[argv.index("-map") + 1] == "0:1"


def test_extract_audio_non_aac_maps_absolute_stream_index(
    mocker: Any, video_file: Path, tmp_path: Path
) -> None:
    """The non-AAC transcode fallback must also use the absolute stream
    index, not an audio-relative index."""

    from video_content_capture.media import audio as audio_mod

    metadata = MediaMetadata(
        source_path=str(video_file),
        container="matroska",
        duration_seconds=10.0,
        video_streams=[_video_stream(index=0, codec="h264")],
        audio_streams=[_audio_stream(index=1, codec="opus")],
        subtitle_streams=[],
    )
    out_path = tmp_path / "out.m4a"

    mock_run = mocker.patch.object(audio_mod.subprocess, "run")
    mock_run.return_value = mocker.Mock(returncode=0, stdout="", stderr="")
    out_path.write_bytes(b"\x00\x00\x00\x18ftypM4A ")
    mocker.patch(
        "video_content_capture.media.audio.probe_video",
        return_value=MediaMetadata(
            source_path=str(out_path),
            container="mov,mp4,m4a,3gp,3g2,mj2",
            duration_seconds=10.0,
            video_streams=[],
            audio_streams=[_audio_stream(index=0, codec="aac")],
            subtitle_streams=[],
        ),
    )

    audio_mod.extract_audio(metadata, out_path)

    argv = list(mock_run.call_args[0][0])
    assert argv[argv.index("-map") + 1] == "0:1"
    assert argv[argv.index("-c:a") + 1] == "aac"


def test_extract_audio_raises_media_error_when_no_audio_stream(
    video_file: Path, tmp_path: Path
) -> None:
    """Extraction from metadata with no audio stream must raise MediaError."""

    from video_content_capture.media.audio import extract_audio

    metadata = MediaMetadata(
        source_path=str(video_file),
        container="mov,mp4,m4a,3gp,3g2,mj2",
        duration_seconds=10.0,
        video_streams=[],
        audio_streams=[],
        subtitle_streams=[],
    )
    with pytest.raises(MediaError, match="(?i)audio"):
        extract_audio(metadata, tmp_path / "out.m4a")


def test_extract_audio_raises_media_error_when_subprocess_fails(
    mocker: Any, video_file: Path, tmp_path: Path
) -> None:
    """ffmpeg nonzero exit must surface as a MediaError."""

    from video_content_capture.media import audio as audio_mod

    metadata = MediaMetadata(
        source_path=str(video_file),
        container="mov,mp4,m4a,3gp,3g2,mj2",
        duration_seconds=10.0,
        video_streams=[],
        audio_streams=[_audio_stream(index=1, codec="aac")],
        subtitle_streams=[],
    )

    mock_run = mocker.patch.object(audio_mod.subprocess, "run")
    mock_run.return_value = mocker.Mock(returncode=1, stdout="", stderr="boom")

    with pytest.raises(MediaError):
        audio_mod.extract_audio(metadata, tmp_path / "out.m4a")


def test_extract_audio_validates_artifact_after_process(
    mocker: Any, video_file: Path, tmp_path: Path
) -> None:
    """After ffmpeg succeeds, the artifact must be probed to validate audio."""

    from video_content_capture.media import audio as audio_mod

    metadata = MediaMetadata(
        source_path=str(video_file),
        container="mov,mp4,m4a,3gp,3g2,mj2",
        duration_seconds=10.0,
        video_streams=[],
        audio_streams=[_audio_stream(index=1, codec="aac")],
        subtitle_streams=[],
    )
    out_path = tmp_path / "out.m4a"

    mock_run = mocker.patch.object(audio_mod.subprocess, "run")
    mock_run.return_value = mocker.Mock(returncode=0, stdout="", stderr="")

    # The artifact file was NOT actually created (out_path does not exist on
    # disk) → validation must fail with a MediaError.
    assert not out_path.exists()

    with pytest.raises(MediaError, match="(?i)artifact|missing|not"):
        audio_mod.extract_audio(metadata, out_path)


def test_extract_audio_rejects_source_mp4_as_upload_artifact(
    mocker: Any, video_file: Path, tmp_path: Path
) -> None:
    """The source MP4 path must never be returned as the extraction artifact."""

    from video_content_capture.media import audio as audio_mod

    metadata = MediaMetadata(
        source_path=str(video_file),
        container="mov,mp4,m4a,3gp,3g2,mj2",
        duration_seconds=10.0,
        video_streams=[],
        audio_streams=[_audio_stream(index=1, codec="aac")],
        subtitle_streams=[],
    )
    out_path = tmp_path / "out.m4a"

    mock_run = mocker.patch.object(audio_mod.subprocess, "run")
    mock_run.return_value = mocker.Mock(returncode=0, stdout="", stderr="")

    out_path.write_bytes(b"\x00\x00\x00\x18ftypM4A ")
    mocker.patch(
        "video_content_capture.media.audio.probe_video",
        return_value=MediaMetadata(
            source_path=str(out_path),
            container="mov,mp4,m4a,3gp,3g2,mj2",
            duration_seconds=10.0,
            video_streams=[],
            audio_streams=[_audio_stream(index=0, codec="aac")],
            subtitle_streams=[],
        ),
    )

    result = audio_mod.extract_audio(metadata, out_path)

    # The returned artifact path is the requested audio output, never the
    # source MP4.
    assert result == out_path
    assert str(result) != metadata.source_path
    assert result.suffix == ".m4a"
    # And the source MP4 path never appears in the ffmpeg argv as the *output*
    # (it appears only as the -i input).
    argv = list(mock_run.call_args[0][0])
    assert "-i" in argv
    input_idx = argv.index("-i")
    assert argv[input_idx + 1] == metadata.source_path
    assert argv[-1] == str(out_path)


# --- CLI probe stub --------------------------------------------------------


def test_cli_probe_reports_metadata_without_credentials(
    mocker: Any, video_file: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """`vcc probe <video>` reports duration/streams without cloud credentials."""

    from video_content_capture import cli as cli_mod
    from video_content_capture.media import probe as probe_mod

    mocker.patch.object(probe_mod.subprocess, "run")
    probe_mod.subprocess.run.return_value = mocker.Mock(stdout=_probe_payload(), returncode=0)

    runner = cli_mod.app
    from typer.testing import CliRunner

    result = CliRunner().invoke(runner, ["probe", str(video_file)])
    assert result.exit_code == 0
    out = result.stdout
    # Exact duration line for ~34:20 (2060.167s).
    assert "duration: 00:34:20" in out
    assert "(2060.167s)" in out
    # Exact stream counts.
    assert "audio streams: 1" in out
    assert "subtitle streams: 0" in out
    assert "video streams: 1" in out


# --- Helpers ---------------------------------------------------------------


def _audio_stream(index: int, codec: str):
    from video_content_capture.domain.models import StreamInfo

    return StreamInfo(
        index=index,
        codec=codec,
        stream_type="audio",
        channels=2,
        sample_rate=44100,
        duration_seconds=10.0,
    )


def _video_stream(index: int, codec: str):
    from video_content_capture.domain.models import StreamInfo

    return StreamInfo(
        index=index,
        codec=codec,
        stream_type="video",
        width=1180,
        height=2556,
        duration_seconds=10.0,
    )
