from pathlib import Path

import pytest

from video_content_capture.workspace.storage import Library
from video_content_capture.workspace.subtitles import parse_subtitles, render_subtitles

FIXTURES = Path(__file__).resolve().parents[1] / "tests" / "fixtures"


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


def test_youtube_header_metadata_block_is_not_a_cue() -> None:
    """The `Kind:`/`Language:` block between the header and cue 1 is not a cue (#13).

    YouTube auto-captions put it there; reading it as a cue tried to parse `Language: en`
    as a timestamp and failed the whole acquisition with `subtitle_acquisition_failed`.
    """
    parsed = parse_subtitles(
        b"WEBVTT\nKind: captions\nLanguage: en\n\n"
        b"00:00:00.080 --> 00:00:03.319 align:start position:0%\nhello there\n\n"
        b"00:00:03.319 --> 00:00:06.200 align:start position:0%\nsecond cue\n",
        "vtt",
        10,
    )
    assert [cue.text for cue in parsed.cues] == ["hello there", "second cue"]
    assert parsed.cues[0].start == 0.08


def test_only_a_well_formed_header_block_is_skipped() -> None:
    """A block that is not header-shaped still reports its own error (#13).

    The fix must not swallow a genuinely malformed file: a first block without `-->` and
    without header syntax has to keep failing, not disappear silently.
    """
    with pytest.raises(ValueError, match="cue 1.*invalid timestamp"):
        parse_subtitles(b"WEBVTT\n\nnot a timestamp\nhello\n", "vtt", 10)
    with pytest.raises(ValueError, match="cue 1.*invalid (timestamp|SRT cue number)"):
        parse_subtitles(b"1\ngarbage\nhello\n", "srt", 10)


def test_youtube_rollup_automatic_captions_parse() -> None:
    """A real-shaped YouTube automatic-caption track parses, text and all (#13).

    This fixture reproduces the four shapes that each broke acquisition on their own: the
    `Kind:`/`Language:` header block, blank roll-up bodies holding a single space, inline
    karaoke timestamps, and a closing cue that drifts past the rounded duration.
    """
    raw = (FIXTURES / "vtt" / "youtube-rollup-automatic.vtt").read_bytes()
    parsed = parse_subtitles(raw, "vtt", 12.0)

    assert [cue.text for cue in parsed.cues] == [
        "a rolling caption keeps",
        "a rolling caption keeps",
        "a rolling caption keeps\nf every word",
        "b after a blank cue",
        "closing cue",
    ]
    # Blank roll-up bodies are not cues, and the header block is not cue 1.
    assert parsed.cues[0].start == 0.64
    assert parsed.cues[0].end == 4.15


def test_no_markup_or_inline_timestamp_reaches_rendered_subtitles() -> None:
    """Nothing the platform embeds inside a cue is burned into the video (#13).

    An inline karaoke timestamp is not valid markup, so an HTML parser keeps it as text;
    left alone it would show up verbatim on screen.
    """
    raw = (FIXTURES / "vtt" / "youtube-rollup-automatic.vtt").read_bytes()
    rendered = render_subtitles(parse_subtitles(raw, "vtt", 12.0).cues, "srt").decode("utf-8")

    assert "<" not in rendered
    assert "00:00:00.799" not in rendered


def test_a_blank_rollup_body_is_skipped_but_empty_markup_is_not() -> None:
    """Only a whitespace-only body is a blank roll-up cue (#13).

    A body that holds markup stripping to nothing still has to fail: those bytes were
    meant to be shown, and silently dropping them would hide a real defect.
    """
    blank = parse_subtitles(
        b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n \n\n00:00:03.000 --> 00:00:04.000\ntext\n",
        "vtt",
        10,
    )
    assert [cue.text for cue in blank.cues] == ["text"]

    with pytest.raises(ValueError, match="empty text"):
        parse_subtitles(
            b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n<script>x()</script>\n", "vtt", 10
        )
    with pytest.raises(ValueError, match="empty text"):
        parse_subtitles(b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\n", "vtt", 10)


def test_a_whitespace_separated_pair_is_still_two_cues() -> None:
    """A blank line that is only whitespace still separates cues (#13).

    The roll-up fix must not run two cues together when their separator is a space.
    """
    parsed = parse_subtitles(
        b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\none\n \n00:00:03.000 --> 00:00:04.000\ntwo\n",
        "vtt",
        10,
    )
    assert [cue.text for cue in parsed.cues] == ["one", "two"]


def test_a_drifting_tail_cue_is_clipped_and_named() -> None:
    """A cue drifting past the rounded duration is clipped, and the warning names it (#13).

    Platform metadata rounds the duration down, so a real track's last cue can run past
    it; a warning pointing at `cue 1` would misdirect whoever reads it.
    """
    parsed = parse_subtitles(
        b"WEBVTT\n\n00:00:01.000 --> 00:00:02.000\none\n\n00:00:03.000 --> 00:00:10.500\ntwo\n",
        "vtt",
        10,
    )
    assert [cue.end for cue in parsed.cues] == [2.0, 10.0]
    assert parsed.warnings == ["cue 2: end clipped to video duration"]

    with pytest.raises(ValueError, match="exceeds video duration"):
        parse_subtitles(b"WEBVTT\n\n00:00:01.000 --> 00:00:30.000\nfar past\n", "vtt", 10)
