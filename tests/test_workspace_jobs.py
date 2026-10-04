from pathlib import Path

import pytest

from video_content_capture.workspace.storage import Library


def test_attempt_gate_rejects_cancel_retry_and_deleting(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video = library.import_video(
        "abcdefghijk", "影片", 10, "https://youtube.com/watch?v=abcdefghijk", "{}"
    )
    assert library.import_video("abcdefghijk", "影片", 10, "url", "{}")["id"] == video["id"]
    job = library.create_job(str(video["id"]), "v1", "a1")
    assert library.create_job(str(video["id"]), "v1", "a1")["id"] == job["id"]
    first = library.start_job(str(job["id"]))
    library.cancel_job(str(job["id"]))
    library.retry_job(str(job["id"]))
    second = library.start_job(str(job["id"]))
    assert first != second
    writes: list[str] = []
    assert not library.publish_attempt(str(job["id"]), first, lambda db: writes.append("old"))
    assert writes == []
    library.mark_deleting(str(video["id"]))
    assert not library.publish_attempt(str(job["id"]), second, lambda db: writes.append("deleted"))
    assert writes == []


def test_job_transitions_restart_and_success(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video = library.import_video("abcdefghijk", "影片", 10, "url", "{}")
    job = library.create_job(str(video["id"]), "v", "a")
    with pytest.raises(ValueError):
        library.retry_job(str(job["id"]))
    attempt = library.start_job(str(job["id"]))
    assert library.publish_attempt(str(job["id"]), attempt, lambda db: None)
    assert library.get_job(str(job["id"]))["status"] == "completed"
    with pytest.raises(ValueError):
        library.retry_job(str(job["id"]))
    other = library.create_job(str(video["id"]), "v2", "a")
    library.start_job(str(other["id"]))
    reopened = Library(library.root)
    reopened.initialize()
    assert reopened.get_job(str(other["id"]))["status"] == "interrupted"


def test_attempt_updates_and_callback_failure_are_fenced(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video = library.import_video("abcdefghijk", "影片", 10, "url", "{}")
    job_id = str(library.create_job(str(video["id"]), "v", "a")["id"])
    first = library.start_job(job_id)
    library.update_attempt(job_id, first, "download", error="network_error")
    assert library.get_job(job_id)["status"] == "failed"
    library.retry_job(job_id)
    second = library.start_job(job_id)
    library.update_attempt(job_id, first, "old", error="late_error")
    assert library.get_job(job_id)["status"] == "running"
    assert library.get_job(job_id)["error_code"] is None
    assert not library.publish_attempt(job_id, second, lambda db: None, lambda db: False)

    def fail(db: object) -> None:
        raise RuntimeError("publication failed")

    with pytest.raises(RuntimeError, match="publication failed"):
        library.publish_attempt(job_id, second, fail)
    assert library.get_job(job_id)["status"] == "running"
    assert library.publish_attempt(job_id, second, lambda db: None)
    assert not library.publish_attempt(job_id, second, lambda db: pytest.fail("late publication"))


def test_one_media_job_runs_and_deleting_blocks_new_work(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    one = library.import_video("abcdefghijk", "first", 10, "url", "{}")
    two = library.import_video("lmnopqrstuv", "second", 10, "url", "{}")
    first = str(library.create_job(str(one["id"]), "v", "a")["id"])
    second = str(library.create_job(str(two["id"]), "v", "a")["id"])
    attempt = library.start_job(first)
    with pytest.raises(ValueError, match="running"):
        library.start_job(second)
    library.cancel_job(first)
    library.start_job(second)
    library.mark_deleting(str(one["id"]))
    with pytest.raises(ValueError):
        library.create_job(str(one["id"]), "v", "a")
    with pytest.raises(ValueError):
        library.retry_job(first)
    assert not library.publish_attempt(
        first, attempt, lambda db: pytest.fail("deleted publication")
    )


def test_playback_position_survives_reopen(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video_id = str(library.import_video("abcdefghijk", "影片", 10, "url", "{}")["id"])
    library.set_position(video_id, 4.25)
    reopened = Library(library.root)
    reopened.initialize()
    assert reopened.get_video(video_id)["position"] == 4.25
    for invalid in (-1.0, float("inf"), float("nan")):
        with pytest.raises(ValueError, match="position"):
            library.set_position(video_id, invalid)


def test_distinct_quality_can_queue_and_metadata_can_refresh(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video_id = str(library.import_video("abcdefghijk", "影片", 10, "url", "{}")["id"])
    one = library.create_job(video_id, "720", "original")
    two = library.create_job(video_id, "1080", "original")
    assert one["id"] != two["id"]
    assert library.create_job(video_id, "720", "original")["id"] == one["id"]
    library.refresh_metadata(video_id, '{"formats": []}')
    assert library.get_video(video_id)["metadata"] == '{"formats": []}'
    with pytest.raises(ValueError, match="position"):
        library.set_position(video_id, 10.01)
    library.set_position(video_id, 10)
    library.mark_deleting(video_id)
    with pytest.raises(ValueError):
        library.refresh_metadata(video_id, "{}")


def test_stage_checkpoint_is_fenced_and_does_not_complete_job(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video_id = str(library.import_video("abcdefghijk", "影片", 10, "url", "{}")["id"])
    job_id = str(library.create_job(video_id, "v", "a")["id"])
    old_attempt = library.start_job(job_id)
    library.cancel_job(job_id)
    library.retry_job(job_id)
    attempt = library.start_job(job_id)
    assert not library.publish_stage(
        job_id,
        old_attempt,
        "video",
        "path",
        "sum",
        "fingerprint",
        lambda db: pytest.fail("stale stage publication"),
    )
    assert library.stage_records(job_id) == []
    assert library.publish_stage(
        job_id,
        attempt,
        "video",
        "path",
        "sum",
        "fingerprint",
        lambda db: None,
    )
    assert library.get_job(job_id)["status"] == "running"
    stage = library.stage_records(job_id)[0]
    assert stage["name"] == "video"
    assert stage["path"] == "path"
    assert stage["checksum"] == "sum"
    assert stage["fingerprint"] == "fingerprint"
    library.mark_deleting(video_id)
    assert not library.publish_stage(
        job_id,
        attempt,
        "audio",
        "path2",
        "sum2",
        "fingerprint",
        lambda db: pytest.fail("deleted stage publication"),
    )


def test_stage_checkpoint_survives_restart_and_failed_checkpoint_rolls_back(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video_id = str(library.import_video("abcdefghijk", "影片", 10, "url", "{}")["id"])
    job_id = str(library.create_job(video_id, "v", "a")["id"])
    attempt = library.start_job(job_id)
    assert library.publish_stage(job_id, attempt, "video", "path", "sum", "fp", lambda db: None)

    def fail(db: object) -> None:
        raise RuntimeError("checkpoint failed")

    with pytest.raises(RuntimeError, match="checkpoint failed"):
        library.publish_stage(job_id, attempt, "audio", "path2", "sum2", "fp", fail)
    assert [stage["name"] for stage in library.stage_records(job_id)] == ["video"]
    reopened = Library(library.root)
    reopened.initialize()
    assert reopened.get_job(job_id)["status"] == "interrupted"
    assert reopened.stage_records(job_id)[0]["checksum"] == "sum"


def test_retry_does_not_duplicate_another_active_same_selection(tmp_path: Path) -> None:
    library = Library(tmp_path / "library")
    library.initialize()
    video = library.import_video("abcdefghijk", "影片", 10, "url", "{}")
    first = library.create_job(str(video["id"]), "v", "a")
    library.cancel_job(str(first["id"]))
    library.create_job(str(video["id"]), "v", "a")
    with pytest.raises(ValueError):
        library.retry_job(str(first["id"]))
