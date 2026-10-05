import sqlite3
from pathlib import Path
from typing import BinaryIO
from uuid import uuid4

import pytest

from video_content_capture.workspace.storage import SCHEMA_VERSION, Library


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
    video_id = str(library.import_video("abcdefghijk", "old", 10, "url", "{}")["id"])
    with sqlite3.connect(library.db_path) as connection:
        connection.execute("DROP INDEX flows_one_running")
        connection.execute("DROP TABLE flows")
        connection.execute("ALTER TABLE jobs DROP COLUMN flow_id")
        connection.execute("PRAGMA user_version = 6")
    upgraded = Library(library.root)
    upgraded.initialize()
    with sqlite3.connect(upgraded.db_path) as connection:
        assert connection.execute("PRAGMA user_version").fetchone()[0] == SCHEMA_VERSION
        assert "flow_id" in {row[1] for row in connection.execute("PRAGMA table_info(jobs)")}
        assert connection.execute("SELECT count(*) FROM flows").fetchone()[0] == 0
    flow = upgraded.create_flow(video_id, "download", {"height": 720})
    assert flow["status"] == "running" and flow["stage"] == "download"
    assert upgraded.get_flow(str(flow["id"]))["snapshot"] == '{"height": 720}'


def test_flow_lifecycle_is_frozen_singular_and_interrupted_on_restart(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video_id = str(library.import_video("abcdefghijk", "影片", 10, "url", "{}")["id"])
    flow = library.create_flow(video_id, "download", {"height": 1080, "target_language": "zh-TW"})
    with pytest.raises(ValueError):
        library.create_flow(video_id, "download", {"height": 720})
    assert [row["id"] for row in library.flows(video_id)] == [flow["id"]]
    assert str(library.active_flow(video_id)["id"]) == flow["id"]  # type: ignore[index]
    assert library.flow_jobs(str(flow["id"])) == []
    job = library.create_job(video_id, "v", "a", str(flow["id"]))
    assert [row["id"] for row in library.flow_jobs(str(flow["id"]))] == [job["id"]]
    advanced = library.update_flow(str(flow["id"]), stage="export")
    assert advanced["stage"] == "export" and advanced["status"] == "running"
    failed = library.update_flow(str(flow["id"]), status="failed", error="media_failed")
    assert failed["status"] == "failed" and failed["error_code"] == "media_failed"
    assert library.active_flow(video_id) is None
    # A finished flow never blocks a new one, and restart interrupts only running flows.
    second = library.create_flow(video_id, "subtitles", {"language": None})
    library.interrupt_running()
    assert library.get_flow(str(second["id"]))["status"] == "interrupted"
    library.mark_deleting(video_id)
    library.purge_video(video_id)
    with sqlite3.connect(library.db_path) as connection:
        assert connection.execute("SELECT count(*) FROM flows").fetchone()[0] == 0
