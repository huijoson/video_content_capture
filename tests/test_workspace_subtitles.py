from pathlib import Path

import pytest

from video_content_capture.workspace.storage import Library
from video_content_capture.workspace.subtitles import parse_subtitles


def test_import_versions_are_immutable_and_selections_independent(tmp_path: Path) -> None:
    library = Library(tmp_path)
    library.initialize()
    video = library.import_video("abcdefghijk", "影片", 10, "https://youtube.com", "{}")
    parsed = parse_subtitles(b"1\n00:00:01,000 --> 00:00:02,000\nHello\n", "srt", 10)
    first = library.create_subtitle_version(str(video["id"]), "en", "same", "import", parsed.cues)
    second = library.create_subtitle_version(str(video["id"]), "en", "same", "import", parsed.cues)
    assert first["id"] != second["id"]
    library.set_position(str(video["id"]), 4)
    library.set_subtitle_selection(str(video["id"]), "playback", str(second["id"]))
    current = library.get_video(str(video["id"]))
    assert current["position"] == 4
    assert current["translation_source_version_id"] is None
    assert library.list_jobs() == []
    assert library.subtitle_cues(str(first["id"]))[0].text == "Hello"


def test_subtitle_parse_clips_and_removes_script() -> None:
    parsed = parse_subtitles(
        b"\xef\xbb\xbfWEBVTT\n\n00:01.000 --> 00:10.500\n<b>Hello</b><script>x()</script>\n",
        "vtt",
        10,
    )
    assert parsed.cues[0].end == 10
    assert parsed.cues[0].text == "Hello"
    assert parsed.warnings


@pytest.mark.parametrize("timing", ["00:02,000 --> 00:01,000", "00:01,000 --> 00:12,000"])
def test_invalid_timing_reports_cue_and_line(timing: str) -> None:
    with pytest.raises(ValueError, match="cue 1.*line 2"):
        parse_subtitles(f"1\n{timing}\nhello".encode(), "srt", 10)


def test_v2_migration_backs_up_without_readding_columns(tmp_path: Path) -> None:
    import sqlite3

    library = Library(tmp_path)
    library.initialize()
    video = library.import_video("abcdefghijk", "old", 10, "https://youtube.com", "{}")
    with sqlite3.connect(library.db_path) as connection:
        for table in ("subtitle_cues", "subtitle_versions", "export_artifacts"):
            connection.execute(f"DROP TABLE {table}")
        for column in (
            "playback_version_id",
            "translation_source_version_id",
            "export_version_id",
            "qa_version_id",
        ):
            connection.execute(f"ALTER TABLE videos DROP COLUMN {column}")
        connection.execute("ALTER TABLE jobs DROP COLUMN snapshot")
        connection.execute("PRAGMA user_version=2")
    library.initialize()
    assert library.get_video(str(video["id"]))["title"] == "old"
    backups = list(tmp_path.glob("*.backup-*"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 2


def test_incomplete_and_other_video_versions_cannot_be_selected(tmp_path: Path) -> None:
    library = Library(tmp_path)
    library.initialize()
    first = library.import_video("abcdefghijk", "one", 10, "https://youtube.com", "{}")
    second = library.import_video("01234567890", "two", 10, "https://youtube.com", "{}")
    version = library.create_subtitle_version(
        str(first["id"]), "zh-TW", "pending", "translation", [], complete=False
    )
    with pytest.raises(ValueError):
        library.set_subtitle_selection(str(first["id"]), "playback", str(version["id"]))
    cues = parse_subtitles(b"1\n00:01,000 --> 00:02,000\nhello", "srt", 10).cues
    complete = library.create_subtitle_version(str(first["id"]), "zh-CN", "same", "import", cues)
    with pytest.raises(ValueError):
        library.set_subtitle_selection(str(second["id"]), "translation_source", str(complete["id"]))


@pytest.mark.parametrize(
    "data,format",
    [
        (b"garbage", "srt"),
        (b"\xff", "srt"),
        (b"1\n00:01,000 --> 00:02,000\n<script>evil()</script>", "srt"),
        (b"wrong header", "vtt"),
        (b"x" * (10 * 1024 * 1024 + 1), "srt"),
    ],
)
def test_invalid_subtitle_is_rejected(data: bytes, format: str) -> None:
    with pytest.raises(ValueError):
        parse_subtitles(data, format, 10)


def test_overlaps_sorted_and_roundtrip() -> None:
    from video_content_capture.workspace.subtitles import render_subtitles

    parsed = parse_subtitles(
        b"1\n00:02,000 --> 00:04,000\nsecond\n\n2\n00:01,000 --> 00:03,000\nfirst", "srt", 10
    )
    assert [cue.text for cue in parsed.cues] == ["first", "second"]
    assert parse_subtitles(render_subtitles(parsed.cues), "vtt", 10).cues[0].text == "first"


def test_multilingual_mlx_preserves_original_text(tmp_path: Path) -> None:
    from threading import Event

    from video_content_capture.workspace.subtitles import MLXSubtitleAdapter

    captured: dict[str, object] = {}

    def fake(*args: object, **kwargs: object) -> object:
        captured.update(kwargs)
        return {
            "language": "en",
            "segments": [{"start": 0, "end": 1, "text": "简体 English 日本語"}],
        }

    language, cues = MLXSubtitleAdapter(fake).transcribe(tmp_path / "media", "en", 10, Event())
    assert language == "en"
    assert cues[0].text == "简体 English 日本語"
    assert "initial_prompt" not in captured
    assert captured["language"] == "en"


@pytest.mark.parametrize(
    "tracks,expected", [([False, True], "platform_manual"), ([True], "platform_auto"), ([], "asr")]
)
def test_acquisition_priority(tracks: list[bool], expected: str, tmp_path: Path) -> None:
    from threading import Event

    from video_content_capture.workspace.subtitles import Cue, acquire_subtitles
    from video_content_capture.workspace.youtube import SourceMetadata, SubtitleTrack

    source = SourceMetadata(
        youtube_id="abcdefghijk",
        source_url="https://youtube.com",
        title="one",
        duration=10,
        formats=[],
        audio_tracks=[],
        subtitles=[
            SubtitleTrack(language="en", automatic=auto, extensions=["vtt"]) for auto in tracks
        ],
        default_format_id="v",
        default_audio_id="a",
        above_1080p=False,
        original_language="en",
    )
    calls: list[str] = []

    class Adapter:
        def query(self, *args: object) -> SourceMetadata:
            calls.append("query")
            return source

        def download_subtitle(self, source: object, track: SubtitleTrack, cancel: object) -> bytes:
            calls.append("auto" if track.automatic else "manual")
            return b"WEBVTT\n\n00:01.000 --> 00:02.000\nhello"

    class ASR:
        def transcribe(self, *args: object) -> tuple[str, list[Cue]]:
            calls.append("asr")
            return "en", [Cue(id="one", start=1, end=2, text="hello")]

    language, kind, _ = acquire_subtitles(Adapter(), ASR(), source, tmp_path / "media", Event())
    assert kind == expected
    assert language == "en"
    assert calls == [
        "query",
        {"platform_manual": "manual", "platform_auto": "auto", "asr": "asr"}[expected],
    ]


def test_caption_list_failure_never_calls_asr(tmp_path: Path) -> None:
    from threading import Event

    from video_content_capture.workspace.subtitles import acquire_subtitles
    from video_content_capture.workspace.youtube import SourceMetadata

    source = SourceMetadata(
        youtube_id="abcdefghijk",
        source_url="https://youtube.com",
        title="one",
        duration=10,
        formats=[],
        audio_tracks=[],
        subtitles=[],
        default_format_id="v",
        default_audio_id="a",
        above_1080p=False,
        original_language="en",
    )

    class Adapter:
        def query(self, *args: object) -> SourceMetadata:
            raise ValueError("temporary list failure")

    class ASR:
        def transcribe(self, *args: object) -> object:
            pytest.fail("Must not infer when list is unavailable")

    with pytest.raises(ValueError, match="temporary list failure"):
        acquire_subtitles(Adapter(), ASR(), source, tmp_path / "media", Event())


def test_srt_rejects_malformed_cue_number() -> None:
    with pytest.raises(ValueError, match="cue 1 line 2"):
        parse_subtitles(b"not-an-index\n00:01,000 --> 00:02,000\nhello", "srt", 10)


@pytest.mark.parametrize("failure", ["checksum", "symlink", "cancel"])
def test_acquisition_rejects_unsafe_asset_and_late_cancel(tmp_path: Path, failure: str) -> None:
    import hashlib
    import sqlite3
    from threading import Event
    from uuid import uuid4

    from video_content_capture.workspace.subtitles import AcquisitionService, Cue
    from video_content_capture.workspace.youtube import SourceMetadata

    library = Library(tmp_path / "library")
    library.initialize()
    source = SourceMetadata(
        youtube_id="abcdefghijk",
        source_url="https://youtube.com",
        title="one",
        duration=10,
        formats=[],
        audio_tracks=[],
        subtitles=[],
        default_format_id="v",
        default_audio_id="a",
        above_1080p=False,
        original_language="en",
    )
    video = library.import_video(
        source.youtube_id,
        source.title,
        source.duration,
        source.source_url,
        source.model_dump_json(),
    )
    video_id = str(video["id"])
    media = library.video_dir(video_id) / "source" / "media.mp4"
    if failure == "symlink":
        outside = tmp_path / "outside.mp4"
        outside.write_bytes(b"media")
        media.symlink_to(outside)
    else:
        media.write_bytes(b"media")
    digest = hashlib.sha256(b"other" if failure == "checksum" else b"media").hexdigest()
    asset_id = uuid4().hex
    with sqlite3.connect(library.db_path) as connection:
        connection.execute(
            "INSERT INTO media_assets VALUES(?,?,?,?,?,?,?,?,?)",
            (
                asset_id,
                video_id,
                "v",
                "a",
                str(media.relative_to(library.root)),
                digest,
                "fp",
                1,
                "mp4",
            ),
        )
    cancel = Event()
    calls: list[str] = []

    class Adapter:
        def query(self, *args: object) -> SourceMetadata:
            return source

    class ASR:
        def transcribe(self, *args: object) -> tuple[str, list[Cue]]:
            calls.append("asr")
            if failure == "cancel":
                cancel.set()
            return "en", [Cue(id="one", start=1, end=2, text="hello")]

    service = AcquisitionService(library, Adapter(), ASR())
    job = service.create(video_id, asset_id)
    attempt = library.start_job(str(job["id"]))
    service.run(job, attempt, cancel)
    assert library.subtitle_versions(video_id) == []
    assert calls == (["asr"] if failure == "cancel" else [])


@pytest.mark.parametrize("failure", ["unknown_language", "disappeared_track"])
def test_ambiguous_or_disappeared_platform_track_never_calls_asr(
    tmp_path: Path, failure: str
) -> None:
    from threading import Event

    from video_content_capture.workspace.subtitles import acquire_subtitles
    from video_content_capture.workspace.youtube import SourceError, SourceMetadata, SubtitleTrack

    track = SubtitleTrack(language="en", automatic=False, extensions=["vtt"])
    source = SourceMetadata(
        youtube_id="abcdefghijk",
        source_url="https://youtube.com",
        title="one",
        duration=10,
        formats=[],
        audio_tracks=[],
        subtitles=[track],
        default_format_id="v",
        default_audio_id="a",
        above_1080p=False,
        original_language=None if failure == "unknown_language" else "en",
    )
    calls: list[str] = []

    class Adapter:
        def query(self, *args: object) -> SourceMetadata:
            return (
                source.model_copy(update={"subtitles": []})
                if failure == "disappeared_track"
                else source
            )

        def download_subtitle(self, *args: object) -> bytes:
            calls.append("download")
            return b"WEBVTT\n\n00:01.000 --> 00:02.000\nhello"

    class ASR:
        def transcribe(self, *args: object) -> object:
            calls.append("asr")
            raise ValueError("ASR should never run")

    with pytest.raises(SourceError, match="choose|unavailable"):
        acquire_subtitles(
            Adapter(),
            ASR(),
            source,
            tmp_path / "media",
            Event(),
            track if failure == "disappeared_track" else None,
        )
    assert calls == []
