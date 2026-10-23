import json
import sqlite3
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

import pytest

from video_content_capture.workspace.storage import SCHEMA_VERSION, Library
from video_content_capture.workspace.subtitles import Cue


def test_library_survives_reopen_and_interrupts_running(tmp_path: Path) -> None:
    root = tmp_path / "library"
    library = Library(root)
    library.initialize()
    with sqlite3.connect(library.db_path) as connection:
        connection.execute(
            "INSERT INTO videos (id, title, created_at, updated_at) VALUES (?, ?, ?, ?)",
            (uuid4().hex, "影片", "before", "before"),
        )
        for status in ("running", "queued", "completed"):
            connection.execute(
                "INSERT INTO jobs (id, kind, status, attempt_id, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?)",
                (uuid4().hex, "download", status, uuid4().hex, "before", "before"),
            )
    reopened = Library(root)
    reopened.initialize()
    assert reopened.list_videos()[0]["title"] == "影片"
    with sqlite3.connect(reopened.db_path) as connection:
        assert {row[0] for row in connection.execute("SELECT status FROM jobs")} == {
            "interrupted",
            "queued",
            "completed",
        }
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
    assert not list(root.glob("*.backup-*"))


def test_migration_backs_up_existing_database(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.root.mkdir()
    with sqlite3.connect(library.db_path) as connection:
        connection.execute("CREATE TABLE preserved (value TEXT)")
        connection.execute("INSERT INTO preserved VALUES ('before')")
    library.initialize()
    backups = list(library.root.glob("*.backup-*"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert connection.execute("SELECT value FROM preserved").fetchone()[0] == "before"
    library.initialize()
    assert list(library.root.glob("*.backup-*")) == backups


def test_newer_schema_is_rejected_without_changes(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.root.mkdir()
    with sqlite3.connect(library.db_path) as connection:
        connection.execute(f"PRAGMA user_version={SCHEMA_VERSION + 1}")
    with pytest.raises(ValueError, match="schema"):
        library.initialize()
    assert not list(library.root.glob("*.backup-*"))


def test_video_directories_use_controlled_ids(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video_id = uuid4().hex
    directory = library.video_dir(video_id)
    assert directory == library.root / "videos" / video_id
    assert {entry.name for entry in directory.iterdir()} == {
        "source",
        "subtitles",
        "previews",
        "exports",
    }
    for invalid in ("../outside", "", "A" * 32, "x" * 32):
        with pytest.raises(ValueError, match="ID"):
            library.video_dir(invalid)
    outside = tmp_path / "outside"
    outside.mkdir()
    second_id = uuid4().hex
    (library.root / "videos" / second_id).symlink_to(outside, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        library.video_dir(second_id)
    assert not list(outside.iterdir())


def test_publish_validates_and_never_overwrites(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video_id = uuid4().hex
    library.video_dir(video_id)
    relative = Path("videos") / video_id / "subtitles" / "one.vtt"

    def validate(path: BinaryIO) -> None:
        assert path.read() == b"WEBVTT\n"
        assert not (library.root / relative).exists()

    target = library.publish(relative, lambda path: path.write(b"WEBVTT\n"), validate)
    assert target.read_bytes() == b"WEBVTT\n"
    with pytest.raises(FileExistsError):
        library.publish(relative, lambda path: path.write(b"replacement"), lambda path: None)
    assert target.read_bytes() == b"WEBVTT\n"
    assert sorted(entry.name for entry in target.parent.iterdir()) == ["one.vtt"]


def test_publish_failure_cleans_temporary_and_rejects_unsafe_paths(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video_id = uuid4().hex
    directory = library.video_dir(video_id)
    relative = Path("videos") / video_id / "source" / "media.mp4"

    def reject(path: BinaryIO) -> None:
        raise ValueError("invalid media")

    with pytest.raises(ValueError, match="invalid media"):
        library.publish(relative, lambda path: path.write(b"bad"), reject)
    assert not list((directory / "source").iterdir())
    for unsafe in (Path("../outside"), tmp_path / "outside"):
        with pytest.raises(ValueError):
            library.publish(unsafe, lambda path: None, lambda path: None)
    outside = tmp_path / "outside"
    outside.mkdir()
    (library.root / "redirect").symlink_to(outside, target_is_directory=True)
    with pytest.raises((ValueError, OSError)):
        library.publish(Path("redirect/file"), lambda path: path.write(b"bad"), reject)
    assert not list(outside.iterdir())


def test_publish_stays_bound_to_open_directory(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video_id = uuid4().hex
    source = library.video_dir(video_id) / "source"
    outside = tmp_path / "outside"
    outside.mkdir()
    moved = source.with_name("original-source")

    def write(path: BinaryIO) -> None:
        source.rename(moved)
        source.symlink_to(outside, target_is_directory=True)
        path.write(b"safe")

    relative = Path("videos") / video_id / "source" / "media.mp4"
    library.publish(relative, write, lambda path: None)
    assert (moved / "media.mp4").read_bytes() == b"safe"
    assert not list(outside.iterdir())


def test_database_and_library_symlinks_are_rejected(tmp_path: Path) -> None:
    outside = tmp_path / "outside"
    outside.mkdir()
    redirected = tmp_path / "redirected"
    redirected.symlink_to(outside, target_is_directory=True)
    with pytest.raises(ValueError, match="symbolic"):
        Library(redirected).initialize()
    library = Library(tmp_path / "library")
    library.root.mkdir()
    target = outside / "database"
    target.write_bytes(b"untouched")
    library.db_path.symlink_to(target)
    with pytest.raises(ValueError, match="symbolic"):
        library.initialize()
    assert target.read_bytes() == b"untouched"


def test_failed_schema_upgrade_is_backed_up_and_atomic(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.root.mkdir()
    with sqlite3.connect(library.db_path) as connection:
        connection.execute("CREATE TABLE jobs (legacy TEXT)")
    with pytest.raises(sqlite3.OperationalError, match="already exists"):
        library.initialize()
    assert len(list(library.root.glob("*.backup-*"))) == 1
    with sqlite3.connect(library.db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 0
        assert (
            connection.execute("SELECT name FROM sqlite_master WHERE name = 'videos'").fetchone()
            is None
        )


def test_new_library_storage_is_private(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    assert library.root.stat().st_mode & 0o777 == 0o700
    assert library.db_path.stat().st_mode & 0o777 == 0o600
    directory = library.video_dir(uuid4().hex)
    assert directory.stat().st_mode & 0o777 == 0o700
    target = library.publish(
        directory.relative_to(library.root) / "source" / "media.mp4",
        lambda temporary: temporary.write(b"media"),
        lambda temporary: None,
    )
    assert target.stat().st_mode & 0o777 == 0o600


def test_v1_upgrade_preserves_records_and_backs_up(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.root.mkdir()
    video_id = uuid4().hex
    with sqlite3.connect(library.db_path) as connection:
        connection.execute(
            "CREATE TABLE videos (id TEXT PRIMARY KEY, title TEXT NOT NULL, "
            "created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute(
            "CREATE TABLE jobs (id TEXT PRIMARY KEY, kind TEXT NOT NULL, status TEXT NOT NULL, "
            "attempt_id TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        connection.execute("INSERT INTO videos VALUES (?, 'old', 'before', 'before')", (video_id,))
        connection.execute("PRAGMA user_version = 1")
    library.initialize()
    assert library.get_video(video_id)["title"] == "old"
    assert library.get_video(video_id)["position"] == 0
    backups = list(library.root.glob("*.backup-*"))
    assert len(backups) == 1
    assert backups[0].stat().st_mode & 0o777 == 0o600
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 1
        assert connection.execute("SELECT title FROM videos").fetchone()[0] == "old"
        assert "youtube_id" not in {
            row[1] for row in connection.execute("PRAGMA table_info(videos)")
        }
    with sqlite3.connect(library.db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert connection.execute("SELECT count(*) FROM media_assets").fetchone()[0] == 0


def test_migration_adds_flow_tracking_to_existing_database(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video_id = str(library.import_video("youtube:abcdefghijk", "old", 10, "url", "{}")["id"])
    with sqlite3.connect(library.db_path) as connection:
        connection.execute("DROP INDEX flows_one_running")
        connection.execute("DROP TABLE flows")
        connection.execute("ALTER TABLE jobs DROP COLUMN flow_id")
        connection.execute("ALTER TABLE videos RENAME COLUMN source_key TO youtube_id")
        connection.execute("DROP INDEX videos_source_key")
        connection.execute("CREATE UNIQUE INDEX videos_youtube_id ON videos (youtube_id)")
        connection.execute(
            "UPDATE videos SET youtube_id=substr(youtube_id, 9) WHERE youtube_id LIKE 'youtube:%'"
        )
        connection.execute("PRAGMA user_version = 6")
    upgraded = Library(library.root)
    upgraded.initialize()
    with sqlite3.connect(upgraded.db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "flow_id" in {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        assert "result" in {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        assert connection.execute("SELECT count(*) FROM flows").fetchone()[0] == 0
    flow = upgraded._create_flow(video_id, "download", {"height": 720})
    assert flow["status"] == "running" and flow["stage"] == "download"
    assert upgraded.get_flow(str(flow["id"]))["snapshot"] == '{"height": 720}'


def test_flow_lifecycle_is_frozen_singular_and_interrupted_on_restart(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video_id = str(library.import_video("youtube:abcdefghijk", "影片", 10, "url", "{}")["id"])
    flow = library._create_flow(video_id, "download", {"height": 1080, "target_language": "zh-TW"})
    with pytest.raises(ValueError):
        library._create_flow(video_id, "download", {"height": 720})
    assert [row["id"] for row in library.flows(video_id)] == [flow["id"]]
    assert str(library.active_flow(video_id)["id"]) == flow["id"]  # type: ignore[index]
    assert library.flow_jobs(str(flow["id"])) == []
    job = library.create_job(video_id, "v", "a", str(flow["id"]))
    assert [row["id"] for row in library.flow_jobs(str(flow["id"]))] == [job["id"]]
    advanced = library._update_flow(str(flow["id"]), stage="export")
    assert advanced["stage"] == "export" and advanced["status"] == "running"
    failed = library._update_flow(str(flow["id"]), status="failed", error="media_failed")
    assert failed["status"] == "failed" and failed["error_code"] == "media_failed"
    assert library.active_flow(video_id) is None
    # A finished flow never blocks a new one, and restart interrupts only running flows.
    second = library._create_flow(video_id, "subtitles", {"language": None})
    library.interrupt_running()
    assert library.get_flow(str(second["id"]))["status"] == "interrupted"
    library.mark_deleting(video_id)
    library.purge_video(video_id)
    with sqlite3.connect(library.db_path) as connection:
        assert connection.execute("SELECT count(*) FROM flows").fetchone()[0] == 0


def test_reopen_flow_revives_only_ended_flows_and_keeps_one_running(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video_id = str(library.import_video("youtube:abcdefghijk", "影片", 10, "url", "{}")["id"])
    flow = library._create_flow(video_id, "subtitles", {"target_language": "en"})
    flow_id = str(flow["id"])

    # Running is idempotent: the coordinator may re-enter retry on a live flow.
    assert str(library._reopen_flow(flow_id)["id"]) == flow_id
    assert str(library._reopen_flow(flow_id)["status"]) == "running"

    library._update_flow(flow_id, status="failed", error="media_failed")
    revived = library._reopen_flow(flow_id)
    assert revived["status"] == "running"
    assert revived["error_code"] is None
    assert str(library.active_flow(video_id)["id"]) == flow_id  # type: ignore[index]

    library._update_flow(flow_id, status="cancelled", error="cancelled")
    assert str(library._reopen_flow(flow_id)["status"]) == "running"
    library.interrupt_running()
    assert str(library._reopen_flow(flow_id)["status"]) == "running"

    # A completed flow is finished for good; retrying it would re-publish an artifact.
    library._update_flow(flow_id, status="completed")
    with pytest.raises(ValueError, match="finished"):
        library._reopen_flow(flow_id)

    with pytest.raises(ValueError):
        library._reopen_flow("0123456789abcdef")


def test_reopen_flow_refuses_while_another_flow_is_running(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video_id = str(library.import_video("youtube:abcdefghijk", "影片", 10, "url", "{}")["id"])
    ended = library._create_flow(video_id, "download", {"target_language": "en"})
    library._update_flow(str(ended["id"]), status="failed", error="media_failed")
    running = library._create_flow(video_id, "download", {"target_language": "en"})

    with pytest.raises(ValueError, match="already in progress"):
        library._reopen_flow(str(ended["id"]))
    assert library.get_flow(str(ended["id"]))["status"] == "failed"
    assert str(library.active_flow(video_id)["id"]) == running["id"]  # type: ignore[index]


def test_latest_flow_returns_the_newest_flow_in_any_status(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video_id = str(library.import_video("youtube:abcdefghijk", "影片", 10, "url", "{}")["id"])
    assert library.latest_flow(video_id) is None

    first = library._create_flow(video_id, "download", {"target_language": "en"})
    library._update_flow(str(first["id"]), status="failed", error="media_failed")
    second = library._create_flow(video_id, "download", {"target_language": "en"})
    assert str(library.latest_flow(video_id)["id"]) == second["id"]  # type: ignore[index]

    library._update_flow(str(second["id"]), status="cancelled")
    latest = library.latest_flow(video_id)
    # A finished flow still leads, which is what the reloaded page needs to label it.
    assert latest is not None and latest["status"] == "cancelled"

    with pytest.raises(ValueError):
        library.latest_flow("0123456789abcdef")


def test_upgrade_shows_existing_subtitles_on_videos_with_no_playback(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    cues = [Cue(id="1", start=1, end=2, text="Hello")]

    def video(source_key: str) -> str:
        return str(library.import_video(source_key, "old", 10, "url", "{}")["id"])

    translated, unfinished, original_only, chosen = (
        video(f"youtube:{letter}bcdefghijk") for letter in "abcd"
    )
    asr = library.create_subtitle_version(translated, "zh", "asr", "asr", cues)
    translation = library.create_subtitle_version(
        translated, "zh-TW", "譯文", "translation", cues, parent_id=str(asr["id"])
    )
    library.create_subtitle_version(
        translated, "en", "pending", "translation", [], parent_id=str(asr["id"]), complete=False
    )
    library.create_subtitle_version(unfinished, "zh", "pending", "asr", [], complete=False)
    imported = library.create_subtitle_version(original_only, "en", "import", "import", cues)
    kept = library.create_subtitle_version(chosen, "zh", "asr", "asr", cues)
    library.create_subtitle_version(
        chosen, "zh-TW", "譯文", "translation", cues, parent_id=str(kept["id"])
    )
    with sqlite3.connect(library.db_path) as connection:
        connection.execute("UPDATE videos SET playback_version_id=NULL WHERE id!=?", (chosen,))
        connection.execute(
            "UPDATE videos SET playback_version_id=? WHERE id=?", (str(kept["id"]), chosen)
        )
        connection.execute("ALTER TABLE videos RENAME COLUMN source_key TO youtube_id")
        connection.execute("DROP INDEX videos_source_key")
        connection.execute("CREATE UNIQUE INDEX videos_youtube_id ON videos (youtube_id)")
        connection.execute(
            "UPDATE videos SET youtube_id=substr(youtube_id, 9) WHERE youtube_id LIKE 'youtube:%'"
        )
        connection.execute("PRAGMA user_version = 7")
    upgraded = Library(library.root)
    upgraded.initialize()
    assert upgraded.get_video(translated)["playback_version_id"] == translation["id"]
    assert upgraded.get_video(unfinished)["playback_version_id"] is None
    assert upgraded.get_video(original_only)["playback_version_id"] == imported["id"]
    assert upgraded.get_video(chosen)["playback_version_id"] == kept["id"]


def test_v9_upgrade_migrates_source_identity_and_export_snapshots(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video = library.import_video(
        "Abc_123-xyz", "影片", 10, "https://www.youtube.com/watch?v=Abc_123-xyz", "{}"
    )
    video_id = str(video["id"])
    metadata = {"youtube_id": "Abc_123-xyz", "title": "影片", "formats": []}
    export_snapshot = {"youtube_id": "Abc_123-xyz", "title": "影片", "height": 720}
    export_job = library.create_snapshot_job(video_id, "export", export_snapshot)
    other_snapshot = {"youtube_id": "preserved", "language": "en"}
    other_job = library.create_snapshot_job(video_id, "subtitles", other_snapshot)
    prefixed = library.import_video("youtube:second", "Second", 10, "second", "{}")
    with sqlite3.connect(library.db_path) as connection:
        connection.execute(
            "UPDATE videos SET metadata=? WHERE id=?", (json.dumps(metadata), video_id)
        )
        connection.execute("ALTER TABLE videos RENAME COLUMN source_key TO youtube_id")
        connection.execute("DROP INDEX videos_source_key")
        connection.execute("CREATE UNIQUE INDEX videos_youtube_id ON videos (youtube_id)")
        connection.execute("PRAGMA user_version = 9")

    upgraded = Library(library.root)
    upgraded.initialize()
    migrated = upgraded.get_video(video_id)
    assert migrated["source_key"] == "youtube:Abc_123-xyz"
    assert "youtube_id" not in migrated
    assert json.loads(str(migrated["metadata"])) == {
        "source_key": "youtube:Abc_123-xyz",
        "site": "YouTube",
        "title": "影片",
        "formats": [],
    }
    assert upgraded.get_video(str(prefixed["id"]))["source_key"] == "youtube:second"
    assert json.loads(str(upgraded.get_job(str(export_job["id"]))["snapshot"])) == {
        "source_key": "youtube:Abc_123-xyz",
        "title": "影片",
        "height": 720,
    }
    assert json.loads(str(upgraded.get_job(str(other_job["id"]))["snapshot"])) == other_snapshot
    with sqlite3.connect(upgraded.db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        indexes = {row[1]: row[2] for row in connection.execute("PRAGMA index_list(videos)")}
        assert indexes["videos_source_key"] == 1
        assert "videos_youtube_id" not in indexes
    backups = list(library.root.glob("*.backup-*"))
    assert len(backups) == 1
    with sqlite3.connect(backups[0]) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == 9
        assert (
            connection.execute("SELECT youtube_id FROM videos WHERE id=?", (video_id,)).fetchone()[
                0
            ]
            == "Abc_123-xyz"
        )
    upgraded.initialize()
    assert upgraded.get_video(video_id)["metadata"] == migrated["metadata"]
    assert list(library.root.glob("*.backup-*")) == backups


def test_source_lookup_deduplicates_sites_and_excludes_deleting(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    url = "https://x.com/example/status/123"
    video = library.import_video("twitter:456", "X clip", 10, url, "{}")
    duplicate = library.import_video("twitter:456", "Other title", 10, url, "{}")
    other_site = library.import_video("youtube:456", "YouTube clip", 10, "other", "{}")
    assert duplicate["id"] == video["id"]
    assert other_site["id"] != video["id"]
    assert library.find_video_by_url(url) == video
    assert library.find_video_by_source_key("twitter:456") == video
    assert library.find_video_by_url("missing") is None
    assert library.find_video_by_source_key("twitter:missing") is None
    library.mark_deleting(str(video["id"]))
    assert library.find_video_by_url(url) is None
    assert library.find_video_by_source_key("twitter:456") is None
