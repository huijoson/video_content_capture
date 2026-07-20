"""Resume-integrity regression tests for OpenSpec group 10 follow-up.

Covers the five required fixes:

1. Completed-step corruption (missing/corrupt/checksum-mismatched completed
   artifacts) MUST NOT silently trigger a paid re-run. A typed
   ``FilesystemError`` propagates with ``--force``/repair guidance (exit 9).
2. Standalone ``report`` uses a separate manifest path so it cannot overwrite
   the full ``run`` manifest. Regression: ``run`` -> standalone ``report`` ->
   ``run --resume`` still skips both paid calls.
3. Real ``python -m video_content_capture`` wiring via subprocess.
4. Standalone ``report --resume`` compares the current transcript checksum
   against ``manifest.content_hash``. A modified transcript + ``--resume``
   raises ``ResumeMismatchError`` (exit 8) and does not return stale report;
   ``--force`` reruns.
5. Multi-artifact checksums: marking transcription/report complete records
   and validates all required durable artifacts (canonical JSON, Markdown,
   raw provider response). Missing/tampered Markdown or raw on a completed
   step surfaces a typed ``FilesystemError`` with force guidance and does NOT
   duplicate the paid call.

All provider/subprocess work is injected/mocked. No network, no real
ffmpeg/ffprobe, no paid cloud calls.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from video_content_capture.config import Settings
from video_content_capture.domain.errors import FilesystemError
from video_content_capture.domain.models import (
    MediaMetadata,
    Report,
    ReportItem,
    ReportSection,
    StreamInfo,
    Transcript,
    TranscriptSegment,
)
from video_content_capture.exit_codes import ExitCode

# ---------------------------------------------------------------------------
# Env isolation
# ---------------------------------------------------------------------------

_CLOUD_ENV_KEYS = (
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
)


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for key in _CLOUD_ENV_KEYS:
        monkeypatch.delenv(key, raising=False)


# ---------------------------------------------------------------------------
# Domain fixture builders (mirror test_cli_integration)
# ---------------------------------------------------------------------------


def _media(source_path: Path | str, duration: float = 20.0) -> MediaMetadata:
    audio = StreamInfo(
        index=1,
        codec="aac",
        stream_type="audio",
        duration_seconds=duration,
        channels=2,
        sample_rate=44100,
    )
    video = StreamInfo(
        index=0,
        codec="hevc",
        stream_type="video",
        duration_seconds=duration,
        width=1180,
        height=2556,
    )
    return MediaMetadata(
        source_path=str(source_path),
        container="mov,mp4,m4a,3gp,3g2,mj2",
        duration_seconds=duration,
        video_streams=[video],
        audio_streams=[audio],
    )


def _segment(
    sid: str, start: float, end: float, text: str, speaker: str = "講者 A"
) -> TranscriptSegment:
    return TranscriptSegment(
        segment_id=sid,
        start=start,
        end=end,
        raw_text=text,
        normalized_text=text,
        speaker_label=speaker,
    )


def _transcript(source: Path, duration: float = 20.0) -> Transcript:
    return Transcript(
        metadata=_media(source, duration),
        segments=[
            _segment("s0001", 0.0, 10.0, "今天談美國聯邦基金利率。"),
            _segment("s0002", 10.0, 20.0, "我認為下半年利率可能維持高檔。", speaker="講者 B"),
        ],
        language="zh-TW",
    )


def _report(transcript: Transcript) -> Report:
    return Report(
        transcript=transcript,
        sections=[
            ReportSection(
                section_type="overview",
                title="三分鐘掌握影片",
                content="摘要。",
                items=[ReportItem(text="摘要。", source_segment_ids=[])],
                source_segment_ids=[],
                is_source_dependent=False,
            ),
            ReportSection(
                section_type="core_topics",
                title="核心重點",
                content="市場利率動向。",
                items=[ReportItem(text="市場利率動向。", source_segment_ids=["s0001"])],
                source_segment_ids=["s0001"],
                is_source_dependent=True,
            ),
            ReportSection(
                section_type="important_numbers",
                title="重要數字與說法",
                content="利率 5.25% 至 5.50%。",
                items=[ReportItem(text="利率 5.25% 至 5.50%。", source_segment_ids=["s0001"])],
                source_segment_ids=["s0001"],
                is_source_dependent=True,
            ),
            ReportSection(
                section_type="glossary",
                title="名詞白話解釋",
                content="聯邦基金利率解釋。",
                items=[
                    ReportItem(
                        label="聯邦基金利率",
                        text="銀行間隔夜借貸目標利率。",
                        source_segment_ids=[],
                    )
                ],
                source_segment_ids=[],
                is_source_dependent=False,
            ),
            ReportSection(
                section_type="conclusion",
                title="結論與可能影響",
                content="講者預期利率維持高檔。",
                items=[
                    ReportItem(
                        text="講者預期利率維持高檔。",
                        source_segment_ids=["s0002"],
                        is_speaker_opinion=True,
                    )
                ],
                source_segment_ids=["s0002"],
                is_source_dependent=True,
            ),
            ReportSection(
                section_type="source_index",
                title="來源索引",
                content="",
                items=[
                    ReportItem(text="s0001", source_segment_ids=["s0001"]),
                    ReportItem(text="s0002", source_segment_ids=["s0002"]),
                ],
                source_segment_ids=["s0001", "s0002"],
                is_source_dependent=False,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Injectable fakes
# ---------------------------------------------------------------------------


class _FakeTranscriber:
    def __init__(self, *, transcript: Transcript, raw_response: bytes = b'{"id":"job-1"}') -> None:
        self._transcript = transcript
        self._raw = raw_response
        self.calls: list[Path] = []

    def transcribe(self, *, audio_path: Path, metadata: MediaMetadata, settings: Settings) -> Any:
        from video_content_capture.transcription.base import TranscriptionResult

        self.calls.append(audio_path)
        return TranscriptionResult(
            transcript=self._transcript,
            raw_text="raw text",
            raw_response=self._raw,
            provider_job_id="job-1",
            chunked=False,
        )


class _FakeReporter:
    def __init__(self, *, report: Report) -> None:
        self._report = report
        self.calls: list[Transcript] = []

    def report(self, *, transcript: Transcript, settings: Settings) -> Any:
        from video_content_capture.reporting.base import ReporterResult

        self.calls.append(transcript)
        return ReporterResult(report=self._report, raw_metadata={"message_id": "msg-1"})


def _build_cli_with_fakes(
    monkeypatch: pytest.MonkeyPatch,
    *,
    source: Path,
    transcript: Transcript | None = None,
    report: Report | None = None,
    transcriber: Any | None = None,
    reporter: Any | None = None,
) -> tuple[Any, Any, Any]:
    if transcript is None:
        transcript = _transcript(source)
    if report is None:
        report = _report(transcript)
    if transcriber is None:
        transcriber = _FakeTranscriber(transcript=transcript)
    if reporter is None:
        reporter = _FakeReporter(report=report)

    from video_content_capture import cli as cli_mod

    def _fake_probe(path: Path) -> MediaMetadata:
        return _media(path)

    def _fake_extract(metadata: MediaMetadata, output_path: Path) -> Path:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-aac-audio")
        return output_path

    monkeypatch.setattr(cli_mod, "_default_probe", _fake_probe)
    monkeypatch.setattr(cli_mod, "_default_extract", _fake_extract)
    monkeypatch.setattr(cli_mod, "_default_transcriber_factory", lambda _s: transcriber)
    monkeypatch.setattr(cli_mod, "_default_reporter_factory", lambda _s: reporter)
    monkeypatch.setattr(cli_mod, "_default_progress", lambda *a, **k: None)

    return cli_mod, transcriber, reporter


def _write_source(tmp_path: Path, name: str = "視野 環球財經.MP4") -> Path:
    src = tmp_path / name
    src.write_bytes(b"fake-mp4-bytes")
    return src


def _full_run(
    runner: CliRunner,
    cli_mod: Any,
    src: Path,
    out_dir: Path,
    *,
    extra: list[str] | None = None,
    env: dict[str, str] | None = None,
) -> Any:
    args = ["run", str(src), "--output-dir", str(out_dir)]
    if extra:
        args.extend(extra)
    full_env = {"ASSEMBLYAI_API_KEY": "aai-key", "ANTHROPIC_API_KEY": "ant-key"}
    if env:
        full_env.update(env)
    return runner.invoke(cli_mod.app, args, env=full_env)


# ---------------------------------------------------------------------------
# Finding 1: completed-step corruption surfaces FilesystemError, no silent re-run
# ---------------------------------------------------------------------------


def test_resume_corrupt_transcript_json_raises_filesystem_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed transcribe step whose canonical JSON is tampered with MUST
    NOT silently re-transcribe. A typed FilesystemError propagates (exit 9)
    with ``--force`` guidance."""

    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    cli_mod, transcriber, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    r1 = _full_run(runner, cli_mod, src, out_dir)
    assert r1.exit_code == 0, r1.output
    assert len(transcriber.calls) == 1

    # Tamper with the canonical transcript JSON.
    transcript_json = out_dir / f"{src.stem}.transcript.json"
    transcript_json.write_text("tampered", encoding="utf-8")

    r2 = _full_run(runner, cli_mod, src, out_dir, extra=["--resume"])
    assert r2.exit_code == int(ExitCode.FILESYSTEM), r2.output
    assert "--force" in r2.output
    assert len(transcriber.calls) == 1, "no silent re-transcription on corruption"


def test_resume_corrupt_manifest_raises_filesystem_error_without_paid_rerun(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """An unreadable existing manifest is not equivalent to no prior state.

    With ``--resume`` the CLI must stop rather than repeat completed paid work.
    """

    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    cli_mod, transcriber, reporter = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    first = _full_run(runner, cli_mod, src, out_dir)
    assert first.exit_code == 0, first.output
    assert len(transcriber.calls) == 1
    assert len(reporter.calls) == 1

    (out_dir / f"{src.stem}.manifest.json").write_text("{ corrupt", encoding="utf-8")

    resumed = _full_run(runner, cli_mod, src, out_dir, extra=["--resume"])
    assert resumed.exit_code == int(ExitCode.FILESYSTEM), resumed.output
    assert "--force" in resumed.output
    assert len(transcriber.calls) == 1
    assert len(reporter.calls) == 1


def test_resume_missing_transcript_markdown_raises_filesystem_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed transcribe step whose Markdown artifact is missing MUST
    surface a FilesystemError (exit 9) with --force guidance, not re-transcribe."""

    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    cli_mod, transcriber, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    r1 = _full_run(runner, cli_mod, src, out_dir)
    assert r1.exit_code == 0, r1.output
    assert len(transcriber.calls) == 1

    # Remove the transcript Markdown (a required durable artifact).
    (out_dir / f"{src.stem}.transcript.md").unlink()

    r2 = _full_run(runner, cli_mod, src, out_dir, extra=["--resume"])
    assert r2.exit_code == int(ExitCode.FILESYSTEM), r2.output
    assert "--force" in r2.output
    assert len(transcriber.calls) == 1


def test_resume_missing_raw_provider_payload_raises_filesystem_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed transcribe step whose raw provider payload is missing MUST
    surface a FilesystemError (exit 9), not re-transcribe."""

    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    cli_mod, transcriber, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    r1 = _full_run(runner, cli_mod, src, out_dir)
    assert r1.exit_code == 0, r1.output

    (out_dir / f"{src.stem}.raw" / "transcribe.json").unlink()

    r2 = _full_run(runner, cli_mod, src, out_dir, extra=["--resume"])
    assert r2.exit_code == int(ExitCode.FILESYSTEM), r2.output
    assert len(transcriber.calls) == 1


def test_resume_corrupt_report_artifact_raises_filesystem_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A completed report step whose report Markdown is tampered MUST surface
    a FilesystemError (exit 9), not re-report."""

    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    cli_mod, _, reporter = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    r1 = _full_run(runner, cli_mod, src, out_dir)
    assert r1.exit_code == 0, r1.output
    assert len(reporter.calls) == 1

    (out_dir / f"{src.stem}.report.md").write_text("tampered", encoding="utf-8")

    r2 = _full_run(runner, cli_mod, src, out_dir, extra=["--resume"])
    assert r2.exit_code == int(ExitCode.FILESYSTEM), r2.output
    assert len(reporter.calls) == 1


# ---------------------------------------------------------------------------
# Finding 2: standalone report manifest collision
# ---------------------------------------------------------------------------


def test_standalone_report_uses_separate_manifest_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Standalone ``report`` writes a ``<stem>.report.manifest.json`` (separate
    from the full ``run`` manifest) so it cannot overwrite run state."""

    from video_content_capture.storage.paths import artifact_paths

    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    paths = artifact_paths(output_dir=out_dir, source_path=src)
    assert paths.report_manifest != paths.manifest
    assert paths.report_manifest.name == f"{src.stem}.report.manifest.json"


def test_run_then_report_then_run_resume_skips_both(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Regression: ``run`` -> standalone ``report`` -> ``run --resume`` still
    skips both paid calls. The standalone report must NOT overwrite the run
    manifest."""

    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    cli_mod, transcriber, reporter = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    # 1. Full run.
    r1 = _full_run(runner, cli_mod, src, out_dir)
    assert r1.exit_code == 0, r1.output
    assert len(transcriber.calls) == 1
    assert len(reporter.calls) == 1

    # 2. Standalone report on the produced transcript (re-runs report by
    #    default since it has its own manifest).
    r2 = runner.invoke(
        cli_mod.app,
        ["report", str(out_dir / f"{src.stem}.transcript.json")],
        env={"ANTHROPIC_API_KEY": "ant-key"},
    )
    assert r2.exit_code == 0, r2.output
    assert len(reporter.calls) == 2, "standalone report re-runs by default"

    # The run manifest must still mark both steps completed.
    run_manifest = json.loads((out_dir / f"{src.stem}.manifest.json").read_text(encoding="utf-8"))
    assert run_manifest["steps"]["transcribe"]["status"] == "completed"
    assert run_manifest["steps"]["report"]["status"] == "completed"

    # 3. run --resume: no new paid calls.
    r3 = _full_run(runner, cli_mod, src, out_dir, extra=["--resume"])
    assert r3.exit_code == 0, r3.output
    assert len(transcriber.calls) == 1, "run --resume must not re-transcribe"
    assert len(reporter.calls) == 2, "run --resume must not re-report"


# ---------------------------------------------------------------------------
# Finding 3: real python -m subprocess test
# ---------------------------------------------------------------------------


def test_python_m_help_via_subprocess(tmp_path: Path) -> None:
    """``python -m video_content_capture --help`` exits 0 and lists commands."""

    env = os.environ.copy()
    # Strip credentials to prove the help path is credential-free.
    for key in _CLOUD_ENV_KEYS:
        env.pop(key, None)
    result = subprocess.run(
        [sys.executable, "-m", "video_content_capture", "--help"],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=30,
    )
    assert result.returncode == 0, result.stderr
    assert "probe" in result.stdout
    assert "transcribe" in result.stdout
    assert "report" in result.stdout
    assert "run" in result.stdout


def test_python_m_probe_via_subprocess(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``python -m video_content_capture probe <video>`` works end-to-end via
    subprocess with a real (mocked-ffprobe) fixture is infeasible in a clean
    subprocess; instead we prove the wiring reaches the probe command and
    surfaces a typed MediaError exit code (2) for a missing file — exercising
    the real ``__main__`` + ``cli`` import chain and the real
    ``probe_video`` -> ``ffprobe`` path without credentials."""

    env = os.environ.copy()
    for key in _CLOUD_ENV_KEYS:
        env.pop(key, None)
    missing = tmp_path / "does-not-exist.MP4"
    result = subprocess.run(
        [sys.executable, "-m", "video_content_capture", "probe", str(missing)],
        capture_output=True,
        text=True,
        env=env,
        cwd=str(tmp_path),
        timeout=30,
    )
    # Missing file -> typed MediaError -> exit 2 (NOT a Typer usage error 2,
    # but the documented media exit code). Both are exit code 2; we assert the
    # media-specific message is present to distinguish.
    assert result.returncode == 2, result.stderr
    assert "not found" in result.stderr or "not found" in result.stdout


# ---------------------------------------------------------------------------
# Finding 4: standalone report --resume transcript identity
# ---------------------------------------------------------------------------


def _write_transcript_artifacts(out_dir: Path, src: Path, transcript: Transcript) -> None:
    from video_content_capture.markdown.transcript import write_transcript_artifacts
    from video_content_capture.storage.paths import artifact_paths

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = artifact_paths(output_dir=out_dir, source_path=src)
    write_transcript_artifacts(paths, transcript)


def test_report_resume_modified_transcript_raises_mismatch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Standalone ``report --resume`` after the transcript JSON changed MUST
    raise ResumeMismatchError (exit 8), not return a stale report."""

    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    transcript = _transcript(src)
    _write_transcript_artifacts(out_dir, src, transcript)

    cli_mod, _, reporter = _build_cli_with_fakes(monkeypatch, source=src, transcript=transcript)

    runner = CliRunner()
    # First report to populate the standalone report manifest.
    r1 = runner.invoke(
        cli_mod.app,
        ["report", str(out_dir / f"{src.stem}.transcript.json")],
        env={"ANTHROPIC_API_KEY": "ant-key"},
    )
    assert r1.exit_code == 0, r1.output
    assert len(reporter.calls) == 1

    # Modify the transcript JSON content (changes checksum / content identity).
    transcript2 = Transcript(
        metadata=transcript.metadata,
        segments=[
            _segment("s0001", 0.0, 10.0, "已修改的逐字稿內容。"),
            _segment("s0002", 10.0, 20.0, "我認為下半年利率可能維持高檔。", speaker="講者 B"),
        ],
        language="zh-TW",
    )
    _write_transcript_artifacts(out_dir, src, transcript2)

    r2 = runner.invoke(
        cli_mod.app,
        ["report", str(out_dir / f"{src.stem}.transcript.json"), "--resume"],
        env={"ANTHROPIC_API_KEY": "ant-key"},
    )
    assert r2.exit_code == int(ExitCode.RESUME_MISMATCH), r2.output
    assert "--force" in r2.output
    assert len(reporter.calls) == 1, "no re-report on mismatch"


def test_report_resume_force_reruns_after_transcript_change(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``report --force`` after a transcript change re-runs the paid report."""

    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    transcript = _transcript(src)
    _write_transcript_artifacts(out_dir, src, transcript)

    cli_mod, _, reporter = _build_cli_with_fakes(monkeypatch, source=src, transcript=transcript)

    runner = CliRunner()
    r1 = runner.invoke(
        cli_mod.app,
        ["report", str(out_dir / f"{src.stem}.transcript.json")],
        env={"ANTHROPIC_API_KEY": "ant-key"},
    )
    assert r1.exit_code == 0, r1.output

    transcript2 = Transcript(
        metadata=transcript.metadata,
        segments=[
            _segment("s0001", 0.0, 10.0, "已修改的逐字稿內容。"),
            _segment("s0002", 10.0, 20.0, "我認為下半年利率可能維持高檔。", speaker="講者 B"),
        ],
        language="zh-TW",
    )
    _write_transcript_artifacts(out_dir, src, transcript2)

    r2 = runner.invoke(
        cli_mod.app,
        ["report", str(out_dir / f"{src.stem}.transcript.json"), "--force"],
        env={"ANTHROPIC_API_KEY": "ant-key"},
    )
    assert r2.exit_code == 0, r2.output
    assert len(reporter.calls) == 2


def test_report_resume_compatible_skips_report(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A compatible ``report --resume`` (unchanged transcript + config) skips
    the paid report step."""

    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    transcript = _transcript(src)
    _write_transcript_artifacts(out_dir, src, transcript)

    cli_mod, _, reporter = _build_cli_with_fakes(monkeypatch, source=src, transcript=transcript)

    runner = CliRunner()
    r1 = runner.invoke(
        cli_mod.app,
        ["report", str(out_dir / f"{src.stem}.transcript.json")],
        env={"ANTHROPIC_API_KEY": "ant-key"},
    )
    assert r1.exit_code == 0, r1.output
    assert len(reporter.calls) == 1

    r2 = runner.invoke(
        cli_mod.app,
        ["report", str(out_dir / f"{src.stem}.transcript.json"), "--resume"],
        env={"ANTHROPIC_API_KEY": "ant-key"},
    )
    assert r2.exit_code == 0, r2.output
    assert len(reporter.calls) == 1, "compatible resume must skip the paid report"


# ---------------------------------------------------------------------------
# Finding 5: multi-artifact checksums — direct pipeline-level tests
# ---------------------------------------------------------------------------


def test_transcribe_step_records_checksums_for_all_required_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest MUST record checksums for the canonical JSON, Markdown, and
    raw provider payload when marking the transcribe step complete."""

    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    transcript = _transcript(src)
    transcriber = _FakeTranscriber(transcript=transcript)
    cli_mod, _, _ = _build_cli_with_fakes(monkeypatch, source=src, transcriber=transcriber)

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.app,
        ["transcribe", str(src), "--output-dir", str(out_dir)],
        env={"ASSEMBLYAI_API_KEY": "aai-key"},
    )
    assert result.exit_code == 0, result.output

    manifest = json.loads((out_dir / f"{src.stem}.manifest.json").read_text(encoding="utf-8"))
    checksums = manifest["artifact_checksums"]
    assert (out_dir / f"{src.stem}.transcript.json").as_posix() in checksums or str(
        out_dir / f"{src.stem}.transcript.json"
    ) in checksums
    # All three required artifacts must have recorded checksums.
    required = [
        out_dir / f"{src.stem}.transcript.json",
        out_dir / f"{src.stem}.transcript.md",
        out_dir / f"{src.stem}.raw" / "transcribe.json",
    ]
    for p in required:
        key = str(p)
        assert key in checksums, f"missing checksum for required artifact {p}"


def test_report_step_records_checksums_for_all_required_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The manifest MUST record checksums for the report JSON, Markdown, and
    raw provider payload when marking the report step complete."""

    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    transcript = _transcript(src)
    _write_transcript_artifacts(out_dir, src, transcript)
    cli_mod, _, _ = _build_cli_with_fakes(monkeypatch, source=src, transcript=transcript)

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.app,
        ["report", str(out_dir / f"{src.stem}.transcript.json")],
        env={"ANTHROPIC_API_KEY": "ant-key"},
    )
    assert result.exit_code == 0, result.output

    manifest = json.loads(
        (out_dir / f"{src.stem}.report.manifest.json").read_text(encoding="utf-8")
    )
    checksums = manifest["artifact_checksums"]
    required = [
        out_dir / f"{src.stem}.report.json",
        out_dir / f"{src.stem}.report.md",
        out_dir / f"{src.stem}.raw" / "report.json",
    ]
    for p in required:
        key = str(p)
        assert key in checksums, f"missing checksum for required artifact {p}"


def test_pipeline_validate_resume_step_raises_filesystem_error_on_tamper(tmp_path: Path) -> None:
    """Direct unit test: ``_validate_resume_step`` raises FilesystemError when
    a required artifact's checksum mismatches, rather than returning False."""

    from video_content_capture.domain.models import ProcessingStepState, RunMetadata, StepStatus
    from video_content_capture.pipeline import Pipeline
    from video_content_capture.storage.manifest import Manifest
    from video_content_capture.storage.paths import artifact_paths

    src = tmp_path / "video.MP4"
    src.write_bytes(b"x")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    paths = artifact_paths(output_dir=out_dir, source_path=src)

    # Create the required artifacts.
    paths.transcript_json.write_text("{}", encoding="utf-8")
    paths.transcript_md.write_text("# md", encoding="utf-8")
    paths.raw_dir.mkdir()
    (paths.raw_dir / "transcribe.json").write_text("{}", encoding="utf-8")

    from video_content_capture.storage.artifacts import compute_checksum

    state = ProcessingStepState(
        step_name="transcribe",
        status=StepStatus.COMPLETED,
        artifact_path=str(paths.transcript_json),
    )
    run = RunMetadata(
        source_path=str(src),
        content_hash="a" * 64,
        config_hash="b" * 64,
        attempt_id="attempt-1",
    )
    manifest = Manifest(
        run_metadata=run,
        content_hash="a" * 64,
        config_hash="b" * 64,
        attempt_id="attempt-1",
        steps={"transcribe": state},
        provider_job_ids={},
        artifact_checksums={
            str(paths.transcript_json): compute_checksum(paths.transcript_json),
            str(paths.transcript_md): compute_checksum(paths.transcript_md),
            str(paths.raw_dir / "transcribe.json"): compute_checksum(
                paths.raw_dir / "transcribe.json"
            ),
        },
    )

    def _noop_probe(_p: Path) -> MediaMetadata:
        return _media(_p)

    def _noop_extract(_m: MediaMetadata, _o: Path) -> Path:
        return _o

    pipeline = Pipeline(
        probe_fn=_noop_probe,
        extract_fn=_noop_extract,
        transcriber_factory=lambda _s: None,  # not used in this unit test
        reporter_factory=lambda _s: None,
        progress=lambda *a, **k: None,
    )

    # Tamper with the Markdown after checksums were recorded.
    paths.transcript_md.write_text("TAMPERED", encoding="utf-8")

    with pytest.raises(FilesystemError):
        pipeline._validate_resume_step(manifest, "transcribe", base_dir=out_dir)


def test_pipeline_rejects_completed_step_without_primary_checksum(tmp_path: Path) -> None:
    """A completed paid step is reusable only when its checksum was recorded."""

    from video_content_capture.domain.models import ProcessingStepState, RunMetadata, StepStatus
    from video_content_capture.pipeline import Pipeline
    from video_content_capture.storage.manifest import Manifest
    from video_content_capture.storage.paths import artifact_paths

    src = tmp_path / "video.MP4"
    src.write_bytes(b"x")
    out_dir = tmp_path / "out"
    out_dir.mkdir()
    paths = artifact_paths(output_dir=out_dir, source_path=src)
    paths.transcript_json.write_text("{}", encoding="utf-8")

    state = ProcessingStepState(
        step_name="transcribe",
        status=StepStatus.COMPLETED,
        artifact_path=str(paths.transcript_json),
    )
    run = RunMetadata(
        source_path=str(src),
        content_hash="a" * 64,
        config_hash="b" * 64,
        attempt_id="attempt-1",
    )
    manifest = Manifest(
        run_metadata=run,
        content_hash="a" * 64,
        config_hash="b" * 64,
        attempt_id="attempt-1",
        steps={"transcribe": state},
        artifact_checksums={},
    )
    pipeline = Pipeline(
        probe_fn=lambda _p: _media(_p),
        extract_fn=lambda _m, output: output,
        transcriber_factory=lambda _s: None,
        reporter_factory=lambda _s: None,
        progress=lambda *args, **kwargs: None,
    )

    with pytest.raises(FilesystemError, match="checksum"):
        pipeline._validate_resume_step(manifest, "transcribe", base_dir=out_dir)
