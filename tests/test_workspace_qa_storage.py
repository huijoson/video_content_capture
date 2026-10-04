import sqlite3
from pathlib import Path

import pytest

from video_content_capture.workspace.storage import Library
from video_content_capture.workspace.subtitles import Cue


def setup_library(tmp_path: Path) -> tuple[Library, str, str]:
    library = Library(tmp_path)
    library.initialize()
    video = library.import_video("abcdefghijk", "video", 10, "https://youtube.com", "{}")
    version = library.create_subtitle_version(
        str(video["id"]),
        "en",
        "Original",
        "platform_manual",
        [Cue(id="s1", start=1, end=2, text="hello")],
    )
    return library, str(video["id"]), str(version["id"])


def test_conversation_selection_preserves_source_and_last_use(tmp_path: Path) -> None:
    library, video, source = setup_library(tmp_path)
    first = library.create_conversation(video)
    translated = library.create_subtitle_version(
        video, "zh-TW", "Translation", "translation", library.subtitle_cues(source), source
    )
    second = library.create_conversation(video, str(translated["id"]))
    assert first["source_version_id"] == source
    assert library.get_video(video)["last_conversation_id"] == second["id"]
    library.select_conversation(video, str(first["id"]))
    assert library.get_video(video)["qa_version_id"] == source
    assert library.get_conversation(str(second["id"]))["source_version_id"] == translated["id"]
    with pytest.raises(ValueError, match="referenced"):
        library.delete_subtitle_version(source)


def test_duplicate_and_late_attempts_are_fenced(tmp_path: Path) -> None:
    library, video, source = setup_library(tmp_path)
    conversation = library.create_conversation(video, source)
    message = library.create_qa_message(str(conversation["id"]), "question", "model")
    with pytest.raises(ValueError, match="pending"):
        library.create_qa_message(str(conversation["id"]), "duplicate", "model")
    job = str(message["request_id"])
    old = library.start_job(job)
    library.cancel_job(job)
    library.retry_job(job)
    new = library.start_job(job)
    assert not library.publish_qa_message(job, old, {"ok": True}, [])
    assert library.publish_qa_message(job, new, {"ok": True}, [])
    assert library.get_qa_message(str(message["id"]))["status"] == "completed"
    assert len(library.qa_memory(str(conversation["id"]))) == 1


def test_delete_and_source_switch_keep_owner_fixed(tmp_path: Path) -> None:
    library, video, source = setup_library(tmp_path)
    first = library.create_conversation(video, source)
    message = library.create_qa_message(str(first["id"]), "question", "model")
    job = str(message["request_id"])
    attempt = library.start_job(job)
    second = library.create_conversation(video, source)
    assert library.publish_qa_message(job, attempt, {"ok": True}, [])
    assert library.qa_messages(str(second["id"])) == []
    retry = library.create_qa_message(str(first["id"]), "late", "model")
    attempt = library.start_job(str(retry["request_id"]))
    library.delete_conversation(str(first["id"]))
    assert not library.publish_qa_message(str(retry["request_id"]), attempt, {"ok": True}, [])


def test_restart_marks_pending_failed_without_requeue(tmp_path: Path) -> None:
    library, video, source = setup_library(tmp_path)
    conversation = library.create_conversation(video, source)
    message = library.create_qa_message(str(conversation["id"]), "question", "model")
    library.initialize()
    assert library.get_qa_message(str(message["id"]))["status"] == "failed"
    assert library.get_job(str(message["request_id"]))["status"] == "interrupted"
    assert library.qa_memory(str(conversation["id"])) == []


def test_v3_migration_preserves_metadata_with_private_backup(tmp_path: Path) -> None:
    library, video, _ = setup_library(tmp_path)
    with sqlite3.connect(library.db_path) as connection:
        connection.execute("DROP TABLE qa_messages")
        connection.execute("DROP TABLE conversations")
        connection.execute("ALTER TABLE videos DROP COLUMN last_conversation_id")
        connection.execute("PRAGMA user_version=3")
    library.initialize()
    assert library.get_video(video)["title"] == "video"
    backups = list(tmp_path.glob("*.backup-*"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 3


def test_deleted_conversation_no_longer_pins_its_source_version(tmp_path: Path) -> None:
    library, video, source = setup_library(tmp_path)
    kept = library.create_subtitle_version(
        video, "en", "Kept", "import", [Cue(id="k1", start=1, end=2, text="kept")]
    )
    active = library.create_conversation(video, str(kept["id"]))
    conversation = library.create_conversation(video, source)
    message = library.create_qa_message(str(conversation["id"]), "question", "model")
    attempt = library.start_job(str(message["request_id"]))
    library.delete_conversation(str(conversation["id"]))
    assert not library.subtitle_version_referenced(source)
    library.delete_subtitle_version(source)
    with pytest.raises(ValueError):
        library.get_subtitle_version(source)
    # The tombstone is gone, yet a late answer still cannot be published anywhere.
    assert not library.publish_qa_message(str(message["request_id"]), attempt, {"ok": True}, [])
    with pytest.raises(ValueError, match="referenced"):
        library.delete_subtitle_version(str(kept["id"]))
    assert library.get_video(video)["last_conversation_id"] == active["id"]
