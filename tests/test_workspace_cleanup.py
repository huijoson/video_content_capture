"""S5 manual cleanup: clear rebuildable previews and delete a whole video safely."""

import sqlite3
from pathlib import Path

import pytest

from video_content_capture.workspace.storage import Library
from video_content_capture.workspace.subtitles import Cue


def seed(tmp_path: Path, youtube_id: str = "abcdefghijk"):
    library = Library(tmp_path / "library")
    library.initialize()
    video_id = str(library.import_video(youtube_id, "影片", 10, "url", "{}")["id"])
    root = library.video_dir(video_id)
    (root / "source" / "asset.mkv").write_bytes(b"s" * 100)
    preview_name = "a" * 32 + ".mp4"
    (root / "previews" / preview_name).write_bytes(b"p" * 50)
    (root / "exports" / "done.mkv").write_bytes(b"e" * 30)
    source = library.create_subtitle_version(
        video_id, "en", "Original", "import", [Cue(id="1", start=0, end=1, text="Hi")]
    )
    library.create_subtitle_version(
        video_id, "zh-TW", "譯文", "translation", [Cue(id="1", start=0, end=1, text="嗨")]
    )
    conversation = library.create_conversation(video_id, str(source["id"]))
    with library._connect() as connection:
        connection.execute(
            "INSERT INTO media_assets VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "asset",
                video_id,
                "v",
                "a",
                f"videos/{video_id}/source/asset.mkv",
                "c",
                "f",
                0,
                "mkv",
            ),
        )
        now = "2026-10-04T00:00:00+00:00"
        for job_id, kind in (("previewjob", "preview"), ("exportjob", "export")):
            connection.execute(
                "INSERT INTO jobs (id,kind,status,created_at,updated_at,video_id,snapshot) "
                "VALUES (?,?,'completed',?,?,?,?)",
                (job_id, kind, now, now, video_id, '{"asset_id": "asset"}'),
            )
        connection.execute(
            "INSERT INTO media_previews (id,video_id,asset_id,job_id,path,checksum,height,"
            "created_at) VALUES (?,?,?,?,?,?,?,?)",
            (
                "a" * 32,
                video_id,
                "asset",
                "previewjob",
                f"videos/{video_id}/previews/{preview_name}",
                "c",
                720,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO export_artifacts VALUES (?,?,?,?,?,?,?)",
            (
                "export",
                video_id,
                "exportjob",
                f"videos/{video_id}/exports/done.mkv",
                "mkv",
                "{}",
                "c",
            ),
        )
    return library, video_id, root, conversation


def test_deletion_scope_counts_every_managed_item_and_size(tmp_path: Path) -> None:
    library, video_id, _, _ = seed(tmp_path)
    scope = library.deletion_scope(video_id)
    assert scope["assets"] == 1
    assert scope["previews"] == 1
    assert scope["subtitle_versions"] == 2
    assert scope["conversations"] == 1
    assert scope["exports"] == 1
    assert scope["size_bytes"] == 180
    assert isinstance(scope["confirmation"], str) and len(str(scope["confirmation"])) == 64
    # The confirmation follows the shown scope; a changed scope requires a new review.
    library.create_subtitle_version(
        video_id, "ja", "日本語", "import", [Cue(id="1", start=0, end=1, text="やあ")]
    )
    assert library.deletion_scope(video_id)["confirmation"] != scope["confirmation"]


def test_clear_previews_keeps_subtitles_conversations_source_and_exports(tmp_path: Path) -> None:
    library, video_id, root, conversation = seed(tmp_path)
    stray = root / "previews" / ("b" * 32 + ".mp4")
    stray.write_bytes(b"orphan from an earlier interrupted clear")
    temporary = root / "previews" / ".preview-running"
    temporary.mkdir()
    result = library.clear_previews(video_id)
    assert result["removed"] == 2
    assert library.previews(video_id) == []
    assert [p.name for p in (root / "previews").iterdir()] == [".preview-running"]
    assert (root / "source" / "asset.mkv").read_bytes() == b"s" * 100
    assert (root / "exports" / "done.mkv").read_bytes() == b"e" * 30
    assert len(library.subtitle_versions(video_id)) == 2
    assert library.get_conversation(str(conversation["id"]))["video_id"] == video_id
    assert len(library.exports(video_id)) == 1
    assert len(library.assets(video_id)) == 1
    # Idempotent: clearing again is safe.
    assert library.clear_previews(video_id)["removed"] == 0


def test_clear_previews_cancels_active_preview_jobs(tmp_path: Path) -> None:
    library, video_id, _, _ = seed(tmp_path)
    job = library.create_snapshot_job(video_id, "preview", {"asset_id": "asset"})
    result = library.clear_previews(video_id)
    assert result["cancelled_jobs"] == [job["id"]]
    assert library.get_job(str(job["id"]))["status"] == "cancelled"


def test_delete_video_marks_deleting_then_removes_files_and_rows(tmp_path: Path) -> None:
    library, video_id, root, _ = seed(tmp_path)
    other_id = str(library.import_video("lmnopqrstuv", "other", 10, "url", "{}")["id"])
    other_root = library.video_dir(other_id)
    (other_root / "source" / "keep.mkv").write_bytes(b"keep")
    running = library.create_job(video_id, "v", "a")
    library.start_job(str(running["id"]))
    active = library.mark_deleting(video_id)
    assert active == [running["id"]]
    assert library.get_job(str(running["id"]))["status"] == "cancelled"
    assert library.list_videos()[0]["id"] == other_id
    assert [v["id"] for v in library.pending_deletions()] == [video_id]
    library.purge_video(video_id)
    assert not root.exists()
    assert library.pending_deletions() == []
    with sqlite3.connect(library.db_path) as connection:
        for table in (
            "media_assets",
            "media_previews",
            "subtitle_versions",
            "conversations",
            "export_artifacts",
            "jobs",
            "videos",
        ):
            count = connection.execute(
                f"SELECT COUNT(*) FROM {table} WHERE video_id=?"
                if table != "videos"
                else "SELECT COUNT(*) FROM videos WHERE id=?",
                (video_id,),
            ).fetchone()[0]
            assert count == 0, table
    assert (other_root / "source" / "keep.mkv").read_bytes() == b"keep"
    # The same YouTube ID can be imported again with a new owner ID.
    again = library.import_video("abcdefghijk", "影片", 10, "url", "{}")
    assert again["id"] != video_id


def test_purge_requires_deleting_mark_and_never_follows_symlinks(tmp_path: Path) -> None:
    library, video_id, root, _ = seed(tmp_path)
    outside = tmp_path / "outside.txt"
    outside.write_text("user file outside the library")
    outside_dir = tmp_path / "outside-dir"
    outside_dir.mkdir()
    (outside_dir / "keep.txt").write_text("keep")
    (root / "exports" / "link.mkv").symlink_to(outside)
    (root / "exports" / "dirlink").symlink_to(outside_dir)
    with pytest.raises(ValueError):
        library.purge_video(video_id)
    assert root.exists()
    library.mark_deleting(video_id)
    library.purge_video(video_id)
    assert outside.read_text() == "user file outside the library"
    assert (outside_dir / "keep.txt").read_text() == "keep"
    assert not root.exists()


@pytest.mark.parametrize("video_id", ["../..", "a" * 31, "../../" + "a" * 32, "A" * 32])
def test_cleanup_rejects_uncontrolled_ids(tmp_path: Path, video_id: str) -> None:
    library, _, _, _ = seed(tmp_path)
    for operation in (library.purge_video, library.deletion_scope, library.clear_previews):
        with pytest.raises(ValueError):
            operation(video_id)


def test_failed_purge_stays_pending_and_can_be_retried(tmp_path: Path, monkeypatch) -> None:
    library, video_id, root, _ = seed(tmp_path)
    library.mark_deleting(video_id)
    original = Library._remove_entry

    def failing(self, parent, name):
        raise OSError("disk busy")

    monkeypatch.setattr(Library, "_remove_entry", failing)
    with pytest.raises(OSError):
        library.purge_video(video_id)
    # Still marked deleting: hidden from the library, shown as pending cleanup, rows intact.
    assert library.list_videos() == []
    assert [v["id"] for v in library.pending_deletions()] == [video_id]
    with pytest.raises(ValueError):
        library.get_video(video_id)
    with pytest.raises(ValueError):
        library.import_video("abcdefghijk", "影片", 10, "url", "{}")
    monkeypatch.setattr(Library, "_remove_entry", original)
    library.purge_video(video_id)
    assert not root.exists()
    assert library.pending_deletions() == []
