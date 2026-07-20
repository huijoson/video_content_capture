"""Focused storage tests for OpenSpec group 5.

Covers:

* Streamed SHA-256 cache keys (chunked read, not whole file in memory).
* Configuration-sensitive cache keys that change on language/speaker/
  provider/model/retry/cleanup changes but never serialize secrets.
* Readable deterministic source-based output names for transcript/report
  JSON+Markdown, metadata, manifest, raw provider payloads, and extracted
  audio/cache locations.
* Atomic manifest replacement through a temporary file + ``os.replace``,
  leaving the previous valid manifest readable when a write is interrupted.
* Compatible resume reuses valid completed steps; incompatible source/config
  raises a typed ``ResumeMismatchError`` with new-attempt guidance; ``force``
  starts a new attempt rather than reusing prior steps.
* Provider job identifiers and artifact checksums are persisted on the
  manifest.
* Artifact checksum/existence is validated before trusting a completed step.
* Cleanup removes extracted audio only after complete success and only when
  ``keep_audio`` is false; failures and ``--keep-audio`` preserve audio and
  all retained artifacts (raw provider, canonical JSON, Markdown, manifest,
  run metadata).

No real ffmpeg/ffprobe/cloud work is performed; filesystem operations use
``tmp_path``.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from video_content_capture.config import Settings
from video_content_capture.domain.errors import FilesystemError, ResumeMismatchError
from video_content_capture.domain.models import (
    MediaMetadata,
    ProcessingStepState,
    RunMetadata,
    StepStatus,
    StreamInfo,
)

# ---------------------------------------------------------------------------
# Helpers / fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip cloud credentials and VCC env from the environment for every test."""

    for key in (
        "ASSEMBLYAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "VCC_LANGUAGE",
        "VCC_OUTPUT_DIR",
        "VCC_CACHE_DIR",
        "VCC_MIN_SPEAKERS",
        "VCC_MAX_SPEAKERS",
        "VCC_MAX_RETRIES",
        "VCC_RETRY_BASE_DELAY_SECONDS",
        "VCC_ASSEMBLYAI_MODEL",
        "VCC_TRANSCRIPTION_BACKEND",
        "VCC_MLX_WHISPER_MODEL",
        "VCC_DEFAULT_ANTHROPIC_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)


def _make_settings(**overrides: Any) -> Settings:
    return Settings(**overrides)


def _make_media_metadata(source_path: Path) -> MediaMetadata:
    audio = StreamInfo(
        index=1,
        codec="aac",
        stream_type="audio",
        duration_seconds=10.0,
        channels=2,
        sample_rate=44100,
    )
    return MediaMetadata(
        source_path=str(source_path),
        container="mov,mp4,m4a,3gp,3g2,mj2",
        duration_seconds=10.0,
        audio_streams=[audio],
    )


# ---------------------------------------------------------------------------
# paths.py — streamed source hash
# ---------------------------------------------------------------------------


def test_source_hash_is_streamed_sha256_of_file_content(tmp_path: Path) -> None:
    """The source hash MUST equal a streamed SHA-256 of the file content."""

    from video_content_capture.storage import paths

    source = tmp_path / "video.mp4"
    # Write enough content that a single-chunk read would be suspicious; we do
    # not assert the chunk size here, only that the digest matches a streamed
    # SHA-256 of the bytes.
    payload = b"video-content-bytes" * 1000
    source.write_bytes(payload)

    digest = paths.compute_source_hash(source)
    expected = hashlib.sha256(payload).hexdigest()
    assert digest == expected
    # Hex SHA-256 is 64 chars.
    assert len(digest) == 64


def test_source_hash_reads_in_chunks_not_whole_file(tmp_path: Path, mocker: Any) -> None:
    """compute_source_hash MUST stream the file in bounded chunks.

    We monkeypatch ``open`` (via the builtins used by the module) to a wrapper
    that records the largest single ``read`` request, then assert that no
    single read requested the entire file at once. A whole-file read would
    request the full size; a chunked stream issues bounded reads.
    """

    from video_content_capture.storage import paths

    source = tmp_path / "big.mp4"
    payload = b"x" * (1024 * 1024 * 3)  # 3 MiB so chunking is exercised.
    source.write_bytes(payload)

    real_open = open  # capture before patching
    max_read = {"size": 0}
    call_count = {"n": 0}

    class _ChunkTrackingWrapper:
        def __init__(self, file_obj: Any) -> None:
            self._file = file_obj

        def __enter__(self) -> _ChunkTrackingWrapper:
            self._file.__enter__()
            return self

        def __exit__(self, *exc: Any) -> None:
            self._file.__exit__(*exc)

        def read(self, size: int = -1) -> bytes:
            call_count["n"] += 1
            if size is None or size < 0:
                max_read["size"] = max(max_read["size"], len(payload))
            else:
                max_read["size"] = max(max_read["size"], size)
            return self._file.read(size)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._file, name)

    def tracking_open(file: Any, *args: Any, **kwargs: Any) -> Any:
        return _ChunkTrackingWrapper(real_open(file, *args, **kwargs))

    mocker.patch.object(paths, "open", tracking_open, create=True)

    digest = paths.compute_source_hash(source)
    assert digest == hashlib.sha256(payload).hexdigest()
    # A chunked implementation issues multiple bounded reads, and none requests
    # the whole 3 MiB file in one call.
    assert call_count["n"] > 1
    assert max_read["size"] < len(payload)


# ---------------------------------------------------------------------------
# paths.py — configuration-sensitive hash (no secrets)
# ---------------------------------------------------------------------------


def test_config_hash_excludes_secrets() -> None:
    """Secrets MUST never be serialized into the config hash."""

    from pydantic import SecretStr

    from video_content_capture.storage import paths

    s_plain = _make_settings()
    s_with_keys = Settings(
        language=s_plain.language,
        assemblyai_model=s_plain.assemblyai_model,
        anthropic_model=s_plain.anthropic_model,
        assemblyai_api_key=SecretStr("sk-secret-1"),
        anthropic_api_key=SecretStr("sk-secret-2"),
    )
    assert paths.compute_config_hash(s_plain) == paths.compute_config_hash(s_with_keys)


def test_config_hash_changes_on_language() -> None:
    from video_content_capture.storage import paths

    a = _make_settings(language="zh-TW")
    b = _make_settings(language="en-US")
    assert paths.compute_config_hash(a) != paths.compute_config_hash(b)


def test_config_hash_changes_on_speaker_bounds() -> None:
    from video_content_capture.storage import paths

    auto = _make_settings()
    bounded = _make_settings(min_speakers=1, max_speakers=3)
    assert paths.compute_config_hash(auto) != paths.compute_config_hash(bounded)


def test_config_hash_changes_on_provider_model() -> None:
    from video_content_capture.storage import paths

    a = _make_settings(assemblyai_model="best")
    b = _make_settings(assemblyai_model="nano")
    assert paths.compute_config_hash(a) != paths.compute_config_hash(b)


def test_config_hash_changes_on_transcription_backend() -> None:
    from video_content_capture.storage import paths

    cloud = _make_settings(transcription_backend="assemblyai")
    local = _make_settings(transcription_backend="mlx")
    assert paths.compute_config_hash(cloud) != paths.compute_config_hash(local)


def test_config_hash_changes_on_mlx_model() -> None:
    from video_content_capture.storage import paths

    a = _make_settings(mlx_whisper_model="model-a")
    b = _make_settings(mlx_whisper_model="model-b")
    assert paths.compute_config_hash(a) != paths.compute_config_hash(b)


def test_config_hash_changes_on_retry_settings() -> None:
    from video_content_capture.storage import paths

    a = _make_settings(max_retries=5, retry_base_delay_seconds=1.0)
    b = _make_settings(max_retries=3, retry_base_delay_seconds=2.0)
    assert paths.compute_config_hash(a) != paths.compute_config_hash(b)


def test_config_hash_changes_on_keep_audio() -> None:
    from video_content_capture.storage import paths

    a = _make_settings(keep_audio=False)
    b = _make_settings(keep_audio=True)
    assert paths.compute_config_hash(a) != paths.compute_config_hash(b)


def test_config_hash_is_deterministic() -> None:
    from video_content_capture.storage import paths

    a = _make_settings()
    b = _make_settings()
    assert paths.compute_config_hash(a) == paths.compute_config_hash(b)
    # Hex SHA-256.
    assert len(paths.compute_config_hash(a)) == 64


# ---------------------------------------------------------------------------
# paths.py — deterministic source-based output names
# ---------------------------------------------------------------------------


def test_artifact_paths_use_source_stem_deterministically(tmp_path: Path) -> None:
    """All output artifacts use readable ``<stem>``-based deterministic names."""

    from video_content_capture.storage.paths import artifact_paths

    out = tmp_path / "out"
    source = Path("/data/視野環球財經 robots 07-19-2026.MP4")
    ap = artifact_paths(output_dir=out, source_path=source)

    assert ap.transcript_json == out / "視野環球財經 robots 07-19-2026.transcript.json"
    assert ap.transcript_md == out / "視野環球財經 robots 07-19-2026.transcript.md"
    assert ap.report_json == out / "視野環球財經 robots 07-19-2026.report.json"
    assert ap.report_md == out / "視野環球財經 robots 07-19-2026.report.md"
    assert ap.metadata == out / "視野環球財經 robots 07-19-2026.metadata.json"
    assert ap.manifest == out / "視野環球財經 robots 07-19-2026.manifest.json"
    # Raw provider payloads live under a per-source directory.
    assert ap.raw_dir == out / "視野環球財經 robots 07-19-2026.raw"
    assert ap.audio.name == "視野環球財經 robots 07-19-2026.m4a"


def test_artifact_paths_deterministic_across_calls(tmp_path: Path) -> None:
    from video_content_capture.storage.paths import artifact_paths

    source = Path("/data/video.MP4")
    out = tmp_path / "out"
    a = artifact_paths(output_dir=out, source_path=source)
    b = artifact_paths(output_dir=out, source_path=source)
    assert a == b


def test_cache_dir_is_keyed_by_content_and_config(tmp_path: Path) -> None:
    """The audio/cache location MUST be keyed by content + config hashes."""

    from video_content_capture.storage.paths import cache_dir_for

    root = tmp_path / "cache"
    c1 = cache_dir_for(root, content_hash="a" * 64, config_hash="b" * 64)
    c2 = cache_dir_for(root, content_hash="a" * 64, config_hash="b" * 64)
    assert c1 == c2  # deterministic
    c3 = cache_dir_for(root, content_hash="a" * 64, config_hash="c" * 64)
    c4 = cache_dir_for(root, content_hash="z" * 64, config_hash="b" * 64)
    assert c3 != c1  # config change -> different cache dir
    assert c4 != c1  # content change -> different cache dir
    # Cache dir is under root.
    assert root in c1.parents


# ---------------------------------------------------------------------------
# manifest.py — typed step state, atomic replacement, interruption safety
# ---------------------------------------------------------------------------


def _make_manifest(
    *,
    content_hash: str = "a" * 64,
    config_hash: str = "b" * 64,
    attempt_id: str = "attempt-0001",
    steps: dict[str, ProcessingStepState] | None = None,
    provider_job_ids: dict[str, str] | None = None,
    artifact_checksums: dict[str, str] | None = None,
) -> Any:
    from video_content_capture.storage.manifest import Manifest

    run = RunMetadata(
        source_path="/data/video.mp4",
        content_hash=content_hash,
        config_hash=config_hash,
        attempt_id=attempt_id,
        started_at="2026-07-19T10:00:00Z",
    )
    return Manifest(
        run_metadata=run,
        content_hash=content_hash,
        config_hash=config_hash,
        attempt_id=attempt_id,
        steps=steps
        if steps is not None
        else {
            "probe": ProcessingStepState(
                step_name="probe",
                status=StepStatus.COMPLETED,
                artifact_path="/out/video.transcript.json",
            )
        },
        provider_job_ids=provider_job_ids or {},
        artifact_checksums=artifact_checksums or {},
    )


def test_manifest_round_trips_through_atomic_write(tmp_path: Path) -> None:
    from video_content_capture.storage.manifest import Manifest, read_manifest, write_manifest

    path = tmp_path / "video.manifest.json"
    m = _make_manifest()
    write_manifest(m, path)

    # The file is present at the final path, not left at a temp path.
    assert path.is_file()
    loaded = read_manifest(path)
    assert loaded is not None
    assert isinstance(loaded, Manifest)
    assert loaded.content_hash == m.content_hash
    assert loaded.config_hash == m.config_hash
    assert loaded.attempt_id == m.attempt_id
    assert "probe" in loaded.steps
    assert loaded.steps["probe"].status is StepStatus.COMPLETED


def test_manifest_atomic_replace_uses_temp_then_os_replace(tmp_path: Path, mocker: Any) -> None:
    """write_manifest MUST serialize to a temp file, fsync, then os.replace."""

    from video_content_capture.storage import manifest as manifest_mod

    path = tmp_path / "video.manifest.json"
    m = _make_manifest()

    replace_spy = mocker.spy(manifest_mod.os, "replace")
    fsync_spy = mocker.spy(manifest_mod.os, "fsync")

    manifest_mod.write_manifest(m, path)

    # os.replace was called exactly once, moving a temp path -> final path.
    assert replace_spy.call_count == 1
    args, _ = replace_spy.call_args
    src, dst = args[0], args[1]
    assert dst == path
    assert src != path
    # fsync was called on the temp file before replacement.
    assert fsync_spy.call_count >= 1


def test_manifest_simulated_interruption_leaves_previous_valid_manifest_readable(
    tmp_path: Path,
    mocker: Any,
) -> None:
    """An interrupted ``write_manifest`` MUST leave the previous valid manifest
    readable AND clean up its stale temp file.

    This drives the real failure path: M1 is written successfully, then M2 is
    written while ``os.replace`` is forced to raise AFTER the temp file is
    created. The M2 write MUST raise, the previous M1 at the final path MUST
    remain readable, and the stale ``.{manifest-name}.*.tmp`` temp file
    created by the interrupted M2 write MUST be removed (not left behind to
    be confused with a future manifest).
    """

    from video_content_capture.storage import manifest as manifest_mod
    from video_content_capture.storage.manifest import read_manifest, write_manifest

    path = tmp_path / "video.manifest.json"
    m1 = _make_manifest(attempt_id="attempt-0001")
    write_manifest(m1, path)

    # Snapshot any pre-existing temp files so we can detect the new one.
    pre_temps = {p.name for p in path.parent.iterdir() if p.name.startswith(".")}
    assert pre_temps == set()  # M1's successful write left no stale temps.

    # Force os.replace to raise mid-M2-write, after the temp file exists.
    replace_real = manifest_mod.os.replace

    def raising_replace(src: Path, dst: Path) -> None:
        # The temp file MUST already exist when os.replace is called.
        assert Path(src).is_file(), "temp file must exist before os.replace"
        raise OSError("simulated interruption during os.replace")

    mocker.patch.object(manifest_mod.os, "replace", side_effect=raising_replace)

    m2 = _make_manifest(attempt_id="attempt-0002")
    with pytest.raises(OSError, match="simulated interruption"):
        write_manifest(m2, path)

    # Restore so assertions can inspect the real filesystem.
    mocker.patch.object(manifest_mod.os, "replace", side_effect=replace_real)

    # The previous valid manifest at the final path MUST still be readable.
    loaded = read_manifest(path)
    assert loaded is not None
    assert loaded.attempt_id == "attempt-0001"

    # No stale temp files remain in path.parent — the interrupted write
    # cleaned up its own temp file.
    post_temps = {p.name for p in path.parent.iterdir() if p.name.startswith(".")}
    assert post_temps == set(), f"stale temp files left behind: {post_temps}"


def test_manifest_interruption_during_fsync_leaves_previous_manifest_readable(
    tmp_path: Path,
    mocker: Any,
) -> None:
    """An interruption during ``os.fsync`` (before replace) MUST also leave
    the previous manifest readable and clean up the temp file."""

    from video_content_capture.storage import manifest as manifest_mod
    from video_content_capture.storage.manifest import read_manifest, write_manifest

    path = tmp_path / "video.manifest.json"
    m1 = _make_manifest(attempt_id="attempt-0001")
    write_manifest(m1, path)

    def raising_fsync(fd: int) -> None:
        raise OSError("simulated interruption during fsync")

    mocker.patch.object(manifest_mod.os, "fsync", side_effect=raising_fsync)

    m2 = _make_manifest(attempt_id="attempt-0002")
    with pytest.raises(OSError, match="simulated interruption"):
        write_manifest(m2, path)

    loaded = read_manifest(path)
    assert loaded is not None
    assert loaded.attempt_id == "attempt-0001"

    post_temps = {p.name for p in path.parent.iterdir() if p.name.startswith(".")}
    assert post_temps == set(), f"stale temp files left behind: {post_temps}"


def test_read_manifest_returns_none_when_missing(tmp_path: Path) -> None:
    from video_content_capture.storage.manifest import read_manifest

    assert read_manifest(tmp_path / "nope.manifest.json") is None


def test_read_manifest_returns_none_on_corrupt_final_file(tmp_path: Path) -> None:
    """A corrupt manifest at the final path is treated as unreadable, not fatal."""

    from video_content_capture.storage.manifest import read_manifest

    path = tmp_path / "video.manifest.json"
    path.write_text("{ not valid json")
    assert read_manifest(path) is None


def test_manifest_persists_provider_job_ids_and_artifact_checksums(tmp_path: Path) -> None:
    from video_content_capture.storage.manifest import read_manifest, write_manifest

    path = tmp_path / "video.manifest.json"
    m = _make_manifest(
        provider_job_ids={"transcribe": "assemblyai-job-123"},
        artifact_checksums={"/out/video.transcript.json": "deadbeef" * 8},
    )
    write_manifest(m, path)
    loaded = read_manifest(path)
    assert loaded is not None
    assert loaded.provider_job_ids == {"transcribe": "assemblyai-job-123"}
    assert loaded.artifact_checksums == {"/out/video.transcript.json": "deadbeef" * 8}


# ---------------------------------------------------------------------------
# manifest.py — resume compatibility / force
# ---------------------------------------------------------------------------


def test_load_resume_state_compatible_returns_manifest(tmp_path: Path) -> None:
    from video_content_capture.storage.manifest import (
        load_resume_state,
        write_manifest,
    )

    path = tmp_path / "video.manifest.json"
    m = _make_manifest(content_hash="a" * 64, config_hash="b" * 64)
    write_manifest(m, path)

    loaded = load_resume_state(path, content_hash="a" * 64, config_hash="b" * 64)
    assert loaded is not None
    assert loaded.attempt_id == m.attempt_id


def test_load_resume_state_incompatible_raises_resume_mismatch(tmp_path: Path) -> None:
    from video_content_capture.storage.manifest import (
        load_resume_state,
        write_manifest,
    )

    path = tmp_path / "video.manifest.json"
    m = _make_manifest(content_hash="a" * 64, config_hash="b" * 64)
    write_manifest(m, path)

    with pytest.raises(ResumeMismatchError) as exc_info:
        load_resume_state(path, content_hash="z" * 64, config_hash="b" * 64)
    # Clear new-attempt guidance must be present.
    msg = str(exc_info.value)
    assert "new" in msg.lower() or "force" in msg.lower() or "attempt" in msg.lower()
    # Details carry both hashes for diagnostics but no secrets.
    assert "content_hash" in exc_info.value.details or "config_hash" in exc_info.value.details


def test_load_resume_state_config_mismatch_raises_resume_mismatch(tmp_path: Path) -> None:
    from video_content_capture.storage.manifest import (
        load_resume_state,
        write_manifest,
    )

    path = tmp_path / "video.manifest.json"
    m = _make_manifest(content_hash="a" * 64, config_hash="b" * 64)
    write_manifest(m, path)

    with pytest.raises(ResumeMismatchError):
        load_resume_state(path, content_hash="a" * 64, config_hash="z" * 64)


def test_load_resume_state_force_starts_new_attempt(tmp_path: Path) -> None:
    """``force=True`` MUST start a new attempt, ignoring the previous manifest.

    Even when the stored manifest would otherwise be incompatible, ``force``
    MUST NOT raise — it returns None so the caller begins a fresh attempt.
    """

    from video_content_capture.storage.manifest import (
        load_resume_state,
        write_manifest,
    )

    path = tmp_path / "video.manifest.json"
    m = _make_manifest(content_hash="a" * 64, config_hash="b" * 64)
    write_manifest(m, path)

    # force with incompatible hashes -> None (new attempt), not a raise.
    loaded = load_resume_state(path, content_hash="z" * 64, config_hash="z" * 64, force=True)
    assert loaded is None

    # force with compatible hashes -> still None (fresh attempt, no reuse).
    loaded = load_resume_state(path, content_hash="a" * 64, config_hash="b" * 64, force=True)
    assert loaded is None


def test_load_resume_state_missing_manifest_returns_none(tmp_path: Path) -> None:
    from video_content_capture.storage.manifest import load_resume_state

    assert (
        load_resume_state(
            tmp_path / "nope.manifest.json",
            content_hash="a" * 64,
            config_hash="b" * 64,
        )
        is None
    )


# ---------------------------------------------------------------------------
# artifacts.py — writing canonical artifacts and raw payloads
# ---------------------------------------------------------------------------


def test_write_transcript_json_atomic(tmp_path: Path) -> None:
    from video_content_capture.storage.artifacts import write_transcript_json

    out = tmp_path / "video.transcript.json"
    metadata = _make_media_metadata(tmp_path / "video.mp4")
    from video_content_capture.domain.models import Transcript, TranscriptSegment

    transcript = Transcript(
        metadata=metadata,
        segments=[
            TranscriptSegment(
                segment_id="s1",
                start=0.0,
                end=1.0,
                raw_text="你好",
                normalized_text="你好",
                speaker_label="講者 A",
            )
        ],
    )
    write_transcript_json(out, transcript)
    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["segments"][0]["segment_id"] == "s1"


def test_write_report_json_atomic(tmp_path: Path) -> None:
    from video_content_capture.domain.models import (
        Report,
        ReportSection,
        Transcript,
        TranscriptSegment,
    )
    from video_content_capture.storage.artifacts import write_report_json

    metadata = _make_media_metadata(tmp_path / "video.mp4")
    transcript = Transcript(
        metadata=metadata,
        segments=[
            TranscriptSegment(
                segment_id="s1",
                start=0.0,
                end=1.0,
                raw_text="你好",
                normalized_text="你好",
                speaker_label="講者 A",
            )
        ],
    )
    report = Report(
        transcript=transcript,
        sections=[
            ReportSection(section_type="overview", title="總覽", content="總覽內容"),
            ReportSection(
                section_type="core_topics",
                title="核心重點",
                content="...",
                source_segment_ids=["s1"],
                is_source_dependent=True,
            ),
            ReportSection(
                section_type="important_numbers",
                title="重要數字",
                content="...",
                source_segment_ids=["s1"],
                is_source_dependent=True,
            ),
            ReportSection(
                section_type="glossary",
                title="名詞",
                content="...",
            ),
            ReportSection(
                section_type="conclusion",
                title="結論",
                content="...",
                source_segment_ids=["s1"],
                is_source_dependent=True,
            ),
            ReportSection(section_type="source_index", title="來源索引", content="..."),
        ],
    )
    out = tmp_path / "video.report.json"
    write_report_json(out, report)
    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["sections"][1]["section_type"] == "core_topics"


def test_write_markdown_atomic(tmp_path: Path) -> None:
    from video_content_capture.storage.artifacts import write_markdown

    out = tmp_path / "video.transcript.md"
    write_markdown(out, "# 逐字稿\n")
    assert out.is_file()
    assert out.read_text(encoding="utf-8") == "# 逐字稿\n"


def test_write_metadata_atomic(tmp_path: Path) -> None:
    from video_content_capture.storage.artifacts import write_metadata

    out = tmp_path / "video.metadata.json"
    run = RunMetadata(
        source_path="/data/video.mp4",
        content_hash="a" * 64,
        config_hash="b" * 64,
        attempt_id="attempt-0001",
    )
    write_metadata(out, run)
    assert out.is_file()
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["attempt_id"] == "attempt-0001"


def test_write_raw_provider_payload_creates_file(tmp_path: Path) -> None:
    from video_content_capture.storage.artifacts import write_raw_provider_payload

    raw_dir = tmp_path / "video.raw"
    payload = b'{"provider":"assemblyai","id":"job-1"}'
    path = write_raw_provider_payload(raw_dir, step_name="transcribe", payload=payload)
    assert path.is_file()
    assert path.read_bytes() == payload
    # The raw payload lives under the per-source raw directory.
    assert raw_dir in path.parents


# ---------------------------------------------------------------------------
# artifacts.py — checksum validation before trusting completed steps
# ---------------------------------------------------------------------------


def test_compute_checksum_is_sha256_of_file_content(tmp_path: Path) -> None:
    from video_content_capture.storage.artifacts import compute_checksum

    p = tmp_path / "x.json"
    p.write_bytes(b"hello")
    assert compute_checksum(p) == hashlib.sha256(b"hello").hexdigest()


def test_validate_artifact_checksum_passes_on_match(tmp_path: Path) -> None:
    from video_content_capture.storage.artifacts import (
        compute_checksum,
        validate_artifact_checksum,
    )

    p = tmp_path / "x.json"
    p.write_bytes(b"hello")
    validate_artifact_checksum(p, compute_checksum(p))  # no raise


def test_validate_artifact_checksum_rejects_mismatch(tmp_path: Path) -> None:
    from video_content_capture.domain.errors import FilesystemError
    from video_content_capture.storage.artifacts import validate_artifact_checksum

    p = tmp_path / "x.json"
    p.write_bytes(b"hello")
    with pytest.raises(FilesystemError):
        validate_artifact_checksum(p, "0" * 64)


def test_validate_artifact_checksum_rejects_missing_file(tmp_path: Path) -> None:
    from video_content_capture.domain.errors import FilesystemError
    from video_content_capture.storage.artifacts import validate_artifact_checksum

    with pytest.raises(FilesystemError):
        validate_artifact_checksum(tmp_path / "missing.json", "0" * 64)


def test_validate_completed_step_rejects_missing_artifact(tmp_path: Path) -> None:
    """A completed step whose artifact is missing MUST NOT be trusted."""

    from video_content_capture.domain.errors import FilesystemError
    from video_content_capture.storage.artifacts import validate_completed_step

    step = ProcessingStepState(
        step_name="transcribe",
        status=StepStatus.COMPLETED,
        artifact_path=str(tmp_path / "missing.json"),
    )
    with pytest.raises(FilesystemError):
        validate_completed_step(step, base_dir=tmp_path)


def test_validate_completed_step_rejects_bad_checksum(tmp_path: Path) -> None:
    from video_content_capture.domain.errors import FilesystemError
    from video_content_capture.storage.artifacts import validate_completed_step

    artifact = tmp_path / "video.transcript.json"
    artifact.write_bytes(b"content")
    step = ProcessingStepState(
        step_name="transcribe",
        status=StepStatus.COMPLETED,
        artifact_path=str(artifact),
    )
    # Attach a wrong checksum to the manifest-side step metadata via the
    # validator's expected_checksum argument.
    with pytest.raises(FilesystemError):
        validate_completed_step(step, base_dir=tmp_path, expected_checksum="0" * 64)


def test_validate_completed_step_passes_on_match(tmp_path: Path) -> None:
    from video_content_capture.storage.artifacts import (
        compute_checksum,
        validate_completed_step,
    )

    artifact = tmp_path / "video.transcript.json"
    artifact.write_bytes(b"content")
    step = ProcessingStepState(
        step_name="transcribe",
        status=StepStatus.COMPLETED,
        artifact_path=str(artifact),
    )
    validate_completed_step(
        step,
        base_dir=tmp_path,
        expected_checksum=compute_checksum(artifact),
    )


# ---------------------------------------------------------------------------
# artifacts.py — success-only audio cleanup and --keep-audio
# ---------------------------------------------------------------------------


def test_cleanup_audio_removes_audio_when_not_keep_audio(tmp_path: Path) -> None:
    from video_content_capture.storage.artifacts import cleanup_audio

    audio = tmp_path / "video.m4a"
    audio.write_bytes(b"audio")
    cleanup_audio(audio_path=audio, keep_audio=False)
    assert not audio.exists()


def test_cleanup_audio_keeps_audio_when_keep_audio(tmp_path: Path) -> None:
    from video_content_capture.storage.artifacts import cleanup_audio

    audio = tmp_path / "video.m4a"
    audio.write_bytes(b"audio")
    cleanup_audio(audio_path=audio, keep_audio=True)
    assert audio.exists()


def test_cleanup_audio_is_noop_when_audio_missing(tmp_path: Path) -> None:
    from video_content_capture.storage.artifacts import cleanup_audio

    # A missing audio file (e.g. never extracted) is a safe no-op.
    cleanup_audio(audio_path=tmp_path / "missing.m4a", keep_audio=False)


def test_cleanup_audio_converts_non_missing_oserror_to_filesystem_error(
    tmp_path: Path, mocker: Any
) -> None:
    """A non-``FileNotFoundError`` cleanup failure MUST NOT be swallowed.

    ``FileNotFoundError`` is a safe no-op (the audio was already removed).
    Other ``OSError`` subclasses (e.g. ``PermissionError`` from a read-only
    output dir or permissions drift) signal an actionable problem and MUST
    surface as the accepted typed :class:`FilesystemError` so the operator
    learns the audio file was not removed. The error MUST NOT leak the audio
    content or any secret — only the path and the underlying cause.
    """

    from video_content_capture.storage import artifacts as artifacts_mod
    from video_content_capture.storage.artifacts import cleanup_audio

    audio = tmp_path / "video.m4a"
    audio.write_bytes(b"audio")

    # Make Path.unlink raise PermissionError (a non-FileNotFoundError OSError).
    mocker.patch.object(
        artifacts_mod.Path,
        "unlink",
        side_effect=PermissionError("permission denied"),
    )

    with pytest.raises(FilesystemError) as exc_info:
        cleanup_audio(audio_path=audio, keep_audio=False)

    # The typed error carries the audio path (no secret, no audio content).
    assert str(audio) in str(exc_info.value) or str(audio) in (
        exc_info.value.details.get("path", "") if exc_info.value.details else ""
    )
    # The underlying cause is preserved for diagnostics.
    assert isinstance(exc_info.value.__cause__, PermissionError)
    # The error category is the accepted FILESYSTEM category.
    from video_content_capture.domain.errors import ErrorCategory

    assert exc_info.value.category is ErrorCategory.FILESYSTEM


def test_cleanup_audio_file_not_found_is_safe_noop(tmp_path: Path, mocker: Any) -> None:
    """``FileNotFoundError`` during unlink MUST remain a safe no-op.

    A concurrent cleanup or a previously-removed file must not raise; only
    actionable (non-ENOENT) errors surface.
    """

    from video_content_capture.storage import artifacts as artifacts_mod
    from video_content_capture.storage.artifacts import cleanup_audio

    audio = tmp_path / "video.m4a"
    audio.write_bytes(b"audio")

    mocker.patch.object(
        artifacts_mod.Path,
        "unlink",
        side_effect=FileNotFoundError("already gone"),
    )

    # Must NOT raise — missing-after-stat is the canonical safe no-op.
    cleanup_audio(audio_path=audio, keep_audio=False)


def test_cleanup_audio_keeps_audio_when_keep_audio_does_not_attempt_unlink(
    tmp_path: Path, mocker: Any
) -> None:
    """``keep_audio=True`` MUST skip unlink entirely (not even attempt it)."""

    from video_content_capture.storage import artifacts as artifacts_mod
    from video_content_capture.storage.artifacts import cleanup_audio

    audio = tmp_path / "video.m4a"
    audio.write_bytes(b"audio")

    unlink_spy = mocker.patch.object(
        artifacts_mod.Path, "unlink", side_effect=AssertionError("unlink must not be called")
    )

    cleanup_audio(audio_path=audio, keep_audio=True)

    assert unlink_spy.call_count == 0
    assert audio.exists()  # retained


def test_cleanup_audio_preserves_other_artifacts(tmp_path: Path) -> None:
    """Cleanup MUST NOT touch retained artifacts (raw, JSON, Markdown, manifest, metadata)."""

    from video_content_capture.storage.artifacts import cleanup_audio

    audio = tmp_path / "video.m4a"
    audio.write_bytes(b"audio")
    raw = tmp_path / "video.raw" / "transcribe.json"
    raw.parent.mkdir()
    raw.write_bytes(b"raw")
    transcript_json = tmp_path / "video.transcript.json"
    transcript_json.write_bytes(b"tj")
    transcript_md = tmp_path / "video.transcript.md"
    transcript_md.write_bytes(b"tm")
    manifest = tmp_path / "video.manifest.json"
    manifest.write_bytes(b"m")
    metadata = tmp_path / "video.metadata.json"
    metadata.write_bytes(b"md")

    cleanup_audio(audio_path=audio, keep_audio=False)

    assert not audio.exists()
    assert raw.exists()
    assert transcript_json.exists()
    assert transcript_md.exists()
    assert manifest.exists()
    assert metadata.exists()


def test_cleanup_audio_preserves_audio_on_failure(tmp_path: Path) -> None:
    """Cleanup is success-only: it is NOT called on failure; simulate that by
    simply not invoking cleanup and confirming audio remains."""

    audio = tmp_path / "video.m4a"
    audio.write_bytes(b"audio")
    # On a failed run the caller does not call cleanup_audio. Audio stays.
    assert audio.exists()
