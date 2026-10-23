import json
import sqlite3
from pathlib import Path

import pytest

from video_content_capture.workspace.storage import Library
from video_content_capture.workspace.subtitle_catalog import SubtitleCatalog
from video_content_capture.workspace.subtitles import Cue

CUES = [Cue(id="one", start=1, end=2, text="hello")]


def setup_library(tmp_path: Path) -> tuple[Library, str, dict, dict]:
    library = Library(tmp_path / "library")
    library.initialize()
    video = library.import_video("youtube:abcdefghijk", "one", 10, "https://youtube.com", "{}")
    first = library.create_subtitle_version(
        str(video["id"]), "en", "first", "platform_manual", CUES
    )
    second = library.create_subtitle_version(
        str(video["id"]), "en", "second", "platform_auto", CUES
    )
    return library, str(video["id"]), first, second


def stale_pointer(library: Library, video_id: str, column: str) -> None:
    with sqlite3.connect(library.db_path) as connection:
        connection.execute(f"UPDATE videos SET {column}='gone' WHERE id=?", (video_id,))


def test_version_for_returns_the_selection_and_clears_a_stale_pointer(tmp_path: Path) -> None:
    library, video_id, first, _ = setup_library(tmp_path)
    catalog = SubtitleCatalog(library)
    library.set_subtitle_selection(video_id, "playback", str(first["id"]))

    assert str(catalog.version_for(video_id, "playback")["id"]) == first["id"]

    stale_pointer(library, video_id, "playback_version_id")
    assert catalog.version_for(video_id, "playback") is None
    assert library.get_video(video_id)["playback_version_id"] is None


def test_recover_clears_every_stale_purpose_but_keeps_live_ones(tmp_path: Path) -> None:
    library, video_id, first, second = setup_library(tmp_path)
    catalog = SubtitleCatalog(library)
    for purpose in ("playback", "export", "translation_source", "qa"):
        library.set_subtitle_selection(video_id, purpose, str(first["id"]))
    stale_pointer(library, video_id, "playback_version_id")
    stale_pointer(library, video_id, "export_version_id")
    library.set_subtitle_selection(video_id, "qa", str(second["id"]))

    catalog.recover(video_id)

    video = library.get_video(video_id)
    assert video["playback_version_id"] is None
    assert video["export_version_id"] is None
    assert video["translation_source_version_id"] == first["id"]
    assert video["qa_version_id"] == second["id"]


def test_record_acquisition_attributes_the_job_and_adopts_only_the_first_source(
    tmp_path: Path,
) -> None:
    library, video_id, first, second = setup_library(tmp_path)
    catalog = SubtitleCatalog(library)

    def publish(version: dict) -> str:
        job = library.create_snapshot_job(
            video_id, "subtitles", {"asset_id": "asset", "language": None, "source_type": None}
        )
        attempt = library.start_job(str(job["id"]))
        assert library.publish_attempt(
            str(job["id"]),
            attempt,
            lambda connection: catalog.record_acquisition(
                connection, video_id, str(job["id"]), str(version["id"])
            ),
        )
        return str(job["id"])

    first_job = publish(first)
    second_job = publish(second)

    assert json.loads(str(library.get_job(first_job)["result"])) == {
        "subtitle_version_id": first["id"]
    }
    assert json.loads(str(library.get_job(second_job)["result"])) == {
        "subtitle_version_id": second["id"]
    }
    video = library.get_video(video_id)
    # The first acquisition pins the source; a later one records provenance but does not
    # silently repoint a choice the user can still edit.
    assert video["translation_source_version_id"] == first["id"]
    assert video["qa_version_id"] == first["id"]


def test_ensure_conversation_opens_with_the_oldest_original(tmp_path: Path) -> None:
    library, video_id, first, _ = setup_library(tmp_path)
    catalog = SubtitleCatalog(library)
    translation = library.create_subtitle_version(
        video_id, "zh-TW", "translated", "translation", CUES, parent_id=str(first["id"])
    )

    conversation = catalog.ensure_conversation(video_id)

    assert conversation is not None
    assert conversation["source_version_id"] == first["id"]
    assert library.get_video(video_id)["qa_version_id"] == first["id"]
    default = catalog.default_source(video_id)
    assert default is not None and str(default["id"]) == first["id"]
    assert library.get_subtitle_version(str(translation["id"]))["source_type"] == "translation"


def test_select_rejects_an_unknown_purpose(tmp_path: Path) -> None:
    library, video_id, first, _ = setup_library(tmp_path)
    catalog = SubtitleCatalog(library)
    with pytest.raises(ValueError, match="selection"):
        catalog.select(video_id, "burned", str(first["id"]))
