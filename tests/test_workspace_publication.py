from pathlib import Path

import pytest

from video_content_capture.workspace.jobs import checksum
from video_content_capture.workspace.publication import Artifact, ArtifactPublisher
from video_content_capture.workspace.storage import Library


def setup_job(tmp_path: Path):
    library = Library(tmp_path / "library")
    library.initialize()
    video = library.import_video("youtube:abcdefghijk", "影片", 10, "url", "{}")
    job = library.create_job(str(video["id"]), "v", "a")
    return library, str(video["id"]), str(job["id"])


def test_gate_publishes_checksums_records_and_releases_stages(tmp_path: Path) -> None:
    library, video_id, job_id = setup_job(tmp_path)
    attempt = library.start_job(job_id)
    stage = tmp_path / "stage.bin"
    stage.write_bytes(b"stage")
    relative_stage = Path("videos") / video_id / "source" / "stage.bin"
    assert library.publish_stage(
        job_id, attempt, "video", str(relative_stage), "sum", "fp", lambda db: None
    )

    source = tmp_path / "artifact.mp4"
    source.write_bytes(b"published bytes")
    relative = Path("videos") / video_id / "exports" / "artifact.mp4"
    seen = []

    def record(db, published):
        seen.extend(published)

    publisher = ArtifactPublisher(library)
    assert publisher.publish(job_id, attempt, [Artifact(relative, source)], record)
    assert (library.root / relative).read_bytes() == b"published bytes"
    assert [item.checksum for item in seen] == [checksum(source)]
    assert library.stage_records(job_id) == []
    assert library.get_job(job_id)["status"] == "completed"


def test_gate_removes_partial_files_when_record_fails(tmp_path: Path) -> None:
    library, video_id, job_id = setup_job(tmp_path)
    attempt = library.start_job(job_id)
    source = tmp_path / "artifact.mp4"
    source.write_bytes(b"published bytes")
    relative = Path("videos") / video_id / "exports" / "artifact.mp4"

    def fail(db, published):
        raise RuntimeError("record failed")

    with pytest.raises(RuntimeError, match="record failed"):
        ArtifactPublisher(library).publish(job_id, attempt, [Artifact(relative, source)], fail)
    assert not (library.root / relative).exists()
    assert library.get_job(job_id)["status"] == "running"


def test_gate_publishes_nothing_for_a_stale_attempt(tmp_path: Path) -> None:
    library, video_id, job_id = setup_job(tmp_path)
    old_attempt = library.start_job(job_id)
    library.cancel_job(job_id)
    library.retry_job(job_id)
    library.start_job(job_id)
    source = tmp_path / "artifact.mp4"
    source.write_bytes(b"published bytes")
    relative = Path("videos") / video_id / "exports" / "artifact.mp4"

    assert not ArtifactPublisher(library).publish(
        job_id, old_attempt, [Artifact(relative, source)], lambda db, published: None
    )
    assert not (library.root / relative).exists()
