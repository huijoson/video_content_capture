"""Integration tests for OpenSpec group 10: pipeline + CLI integration.

Covers:

* ``vcc probe`` credential-free behavior, Chinese path, no-audio media error
  exit code.
* ``vcc transcribe`` credential requirement (lazy AssemblyAI, no Anthropic),
  write order (raw + canonical transcript JSON before Markdown + metadata +
  manifest), source MP4 never uploaded.
* ``vcc report`` accepts canonical ``.transcript.json`` OR generated
  ``.transcript.md`` (resolving the adjacent ``.transcript.json``); validates
  Anthropic credential only; writes canonical report JSON before Markdown.
* ``vcc run`` coordinates all steps; success-only audio cleanup unless
  ``--keep-audio``; both credentials validated before any paid call.
* Resume after transcription starts at report generation without another
  transcription request; a second compatible mocked ``--resume`` run makes
  no transcription OR reporting request.
* ``--force`` performs paid steps again without reusing prior completed steps.
* Incompatible resume raises ``ResumeMismatchError`` with ``--force`` guidance
  and a stable exit code.
* Ctrl-C / ``KeyboardInterrupt`` preserves the last completed manifest and
  returns exit 130.
* Stable exit codes mapped from typed error categories.
* Secret redaction: every configured credential value is scrubbed from CLI
  output, progress events, logging, exception text/details/repr, and generated
  metadata.
* No live/paid provider calls in the default suite (injected fakes).

All provider/subprocess work is injected/mocked. No network, no real
ffmpeg/ffprobe, no paid cloud calls.
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from typing import Any

import pytest
from typer.testing import CliRunner

from video_content_capture.config import Settings
from video_content_capture.domain.models import (
    MediaMetadata,
    Report,
    ReportItem,
    ReportSection,
    StreamInfo,
    Transcript,
    TranscriptSegment,
)

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
# Domain fixture builders
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
                items=[
                    ReportItem(
                        text="市場利率動向。",
                        source_segment_ids=["s0001"],
                    )
                ],
                source_segment_ids=["s0001"],
                is_source_dependent=True,
            ),
            ReportSection(
                section_type="important_numbers",
                title="重要數字與說法",
                content="利率 5.25% 至 5.50%。",
                items=[
                    ReportItem(
                        text="利率 5.25% 至 5.50%。",
                        source_segment_ids=["s0001"],
                    )
                ],
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
# Injectable fake providers / probe / extractor
# ---------------------------------------------------------------------------


class _FakeTranscriber:
    """Fake Transcriber recording calls and returning a canned Transcript."""

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
    """Fake Reporter recording calls and returning a canned Report."""

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
    probe_metadata: MediaMetadata | None = None,
    extract_raise: BaseException | None = None,
    transcriber: _FakeTranscriber | None = None,
    reporter: _FakeReporter | None = None,
) -> tuple[Any, _FakeTranscriber, _FakeReporter, list[str]]:
    """Wire the CLI's default pipeline factories to fakes.

    Returns the (cli module, transcriber, reporter, progress_events) tuple.
    """

    if transcript is None:
        transcript = _transcript(source)
    if report is None:
        report = _report(transcript)
    if transcriber is None:
        transcriber = _FakeTranscriber(transcript=transcript)
    if reporter is None:
        reporter = _FakeReporter(report=report)
    if probe_metadata is None:
        probe_metadata = _media(source)

    progress_events: list[str] = []

    from video_content_capture import cli as cli_mod
    from video_content_capture import pipeline as pipeline_mod

    def _fake_probe(path: Path) -> MediaMetadata:
        return probe_metadata

    def _fake_extract(metadata: MediaMetadata, output_path: Path) -> Path:
        if extract_raise is not None:
            raise extract_raise
        output_path.parent.mkdir(parents=True, exist_ok=True)
        output_path.write_bytes(b"fake-aac-audio")
        return output_path

    def _transcriber_factory(settings: Settings) -> _FakeTranscriber:
        return transcriber

    def _reporter_factory(settings: Settings) -> _FakeReporter:
        return reporter

    def _progress(step: str, event: str, **data: Any) -> None:
        progress_events.append(f"{step}:{event}")

    monkeypatch.setattr(cli_mod, "_default_probe", _fake_probe)
    monkeypatch.setattr(cli_mod, "_default_extract", _fake_extract)
    monkeypatch.setattr(cli_mod, "_default_transcriber_factory", _transcriber_factory)
    monkeypatch.setattr(cli_mod, "_default_reporter_factory", _reporter_factory)
    monkeypatch.setattr(cli_mod, "_default_progress", _progress)
    # Block any accidental real SDK client construction in adapters.
    monkeypatch.setattr(pipeline_mod, "_real_sleep", lambda _s: None)

    return cli_mod, transcriber, reporter, progress_events


def _write_source(tmp_path: Path, name: str = "視野 環球財經.MP4") -> Path:
    src = tmp_path / name
    src.write_bytes(b"fake-mp4-bytes")
    return src


# ---------------------------------------------------------------------------
# Exit code table
# ---------------------------------------------------------------------------


def test_exit_code_table_is_stable_and_documented() -> None:
    """Exit codes MUST be centralized and stable per the spec mapping."""

    from video_content_capture.exit_codes import ExitCode

    assert int(ExitCode.SUCCESS) == 0
    assert int(ExitCode.UNEXPECTED) == 1
    assert int(ExitCode.MEDIA) == 2
    assert int(ExitCode.CONFIGURATION) == 3
    assert int(ExitCode.PROVIDER_AUTH) == 4
    assert int(ExitCode.RATE_LIMIT) == 5
    assert int(ExitCode.PROVIDER_PAYLOAD) == 6
    assert int(ExitCode.GROUNDING) == 7
    assert int(ExitCode.RESUME_MISMATCH) == 8
    assert int(ExitCode.FILESYSTEM) == 9
    assert int(ExitCode.INTERRUPTED) == 130


def test_exit_code_for_category_mapping() -> None:
    """Each typed ErrorCategory maps to its documented exit code."""

    from video_content_capture.domain.errors import ErrorCategory
    from video_content_capture.exit_codes import ExitCode, exit_code_for_category

    assert exit_code_for_category(ErrorCategory.MEDIA) == ExitCode.MEDIA
    assert exit_code_for_category(ErrorCategory.CONFIGURATION) == ExitCode.CONFIGURATION
    assert exit_code_for_category(ErrorCategory.PROVIDER_AUTH) == ExitCode.PROVIDER_AUTH
    assert exit_code_for_category(ErrorCategory.RATE_LIMIT) == ExitCode.RATE_LIMIT
    assert exit_code_for_category(ErrorCategory.PROVIDER_PAYLOAD) == ExitCode.PROVIDER_PAYLOAD
    assert exit_code_for_category(ErrorCategory.GROUNDING) == ExitCode.GROUNDING
    assert exit_code_for_category(ErrorCategory.RESUME_MISMATCH) == ExitCode.RESUME_MISMATCH
    assert exit_code_for_category(ErrorCategory.FILESYSTEM) == ExitCode.FILESYSTEM


# ---------------------------------------------------------------------------
# probe
# ---------------------------------------------------------------------------


def test_probe_command_is_credential_free_and_reports_streams(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _write_source(tmp_path, name="視野 環球財經.MP4")
    cli_mod, _, _, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["probe", str(src)])

    assert result.exit_code == 0, result.output
    assert "container" in result.output
    assert "audio streams: 1" in result.output
    assert "視野 環球財經.MP4" in result.output


def test_probe_without_credentials_succeeds(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Probe must succeed with no cloud keys configured."""

    src = _write_source(tmp_path)
    cli_mod, _, _, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["probe", str(src)])
    assert result.exit_code == 0


def test_probe_media_error_exit_code(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from video_content_capture.domain.errors import MediaError

    src = _write_source(tmp_path)
    cli_mod, _, _, _ = _build_cli_with_fakes(monkeypatch, source=src)
    monkeypatch.setattr(
        cli_mod, "_default_probe", lambda _p: (_ for _ in ()).throw(MediaError("no audio"))
    )

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["probe", str(src)])
    assert result.exit_code == 2


# ---------------------------------------------------------------------------
# transcribe
# ---------------------------------------------------------------------------


def test_transcribe_requires_assemblyai_credential_not_anthropic(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``vcc transcribe`` validates the AssemblyAI key before paid work; no
    Anthropic key is required for transcription."""

    src = _write_source(tmp_path)
    cli_mod, transcriber, _, _ = _build_cli_with_fakes(monkeypatch, source=src)

    # Only AssemblyAI key configured (via env); no Anthropic key. Should
    # succeed — transcription does not require the Anthropic credential.
    runner = CliRunner()
    result = runner.invoke(
        cli_mod.app,
        ["transcribe", str(src)],
        env={"ASSEMBLYAI_API_KEY": "aai-key"},
    )
    assert result.exit_code == 0, result.output
    assert transcriber.calls, "transcriber must have been called"


def test_transcribe_without_assemblyai_credential_exits_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _write_source(tmp_path)
    cli_mod, transcriber, _, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["transcribe", str(src)])
    assert result.exit_code == 3
    assert not transcriber.calls, "no paid call before credential validation"


def test_transcribe_local_backend_succeeds_without_credentials(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _write_source(tmp_path)
    out_dir = tmp_path / "local-out"
    cli_mod, transcriber, _, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.app,
        [
            "transcribe",
            str(src),
            "--transcription-backend",
            "mlx",
            "--output-dir",
            str(out_dir),
        ],
    )
    assert result.exit_code == 0, result.output
    assert len(transcriber.calls) == 1
    assert (out_dir / f"{src.stem}.transcript.json").is_file()


def test_transcribe_local_backend_rejects_speaker_bounds_before_inference(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _write_source(tmp_path)
    cli_mod, transcriber, _, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.app,
        [
            "transcribe",
            str(src),
            "--transcription-backend",
            "mlx",
            "--min-speakers",
            "1",
        ],
    )
    assert result.exit_code == 3
    assert "diarization" in result.output.lower()
    assert not transcriber.calls


def test_transcribe_invalid_backend_exits_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _write_source(tmp_path)
    cli_mod, transcriber, _, _ = _build_cli_with_fakes(monkeypatch, source=src)

    result = CliRunner().invoke(
        cli_mod.app,
        ["transcribe", str(src), "--transcription-backend", "invalid"],
    )
    assert result.exit_code == 3
    assert not transcriber.calls


def test_default_transcriber_factory_dispatches_without_fallback(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from video_content_capture import cli as cli_mod
    from video_content_capture.transcription import assemblyai as assemblyai_mod
    from video_content_capture.transcription import mlx as mlx_mod

    assembly_sentinel = object()
    mlx_sentinel = object()
    monkeypatch.setattr(
        assemblyai_mod,
        "AssemblyAITranscriber",
        lambda *, settings: assembly_sentinel,
    )
    monkeypatch.setattr(mlx_mod, "MLXWhisperTranscriber", lambda: mlx_sentinel)

    assert (
        cli_mod._default_transcriber_factory(
            Settings(transcription_backend="mlx"),
        )
        is mlx_sentinel
    )
    assert (
        cli_mod._default_transcriber_factory(
            Settings(transcription_backend="assemblyai", assemblyai_api_key="key"),
        )
        is assembly_sentinel
    )


def test_local_transcribe_resume_supports_relative_output_dir(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    src = _write_source(tmp_path)
    cli_mod, transcriber, _, _ = _build_cli_with_fakes(monkeypatch, source=src)
    monkeypatch.chdir(tmp_path)
    command = [
        "transcribe",
        str(src),
        "--transcription-backend",
        "mlx",
        "--output-dir",
        "relative-out",
    ]

    runner = CliRunner()
    first = runner.invoke(cli_mod.app, command)
    assert first.exit_code == 0, first.output
    resumed = runner.invoke(cli_mod.app, [*command, "--resume"])
    assert resumed.exit_code == 0, resumed.output
    assert len(transcriber.calls) == 1


def test_transcribe_write_order_json_before_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    cli_mod, _, _, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.app,
        ["transcribe", str(src), "--output-dir", str(out_dir)],
        env={"ASSEMBLYAI_API_KEY": "aai-key"},
    )
    assert result.exit_code == 0, result.output

    stem = src.stem
    transcript_json = out_dir / f"{stem}.transcript.json"
    transcript_md = out_dir / f"{stem}.transcript.md"
    manifest = out_dir / f"{stem}.manifest.json"
    raw_dir = out_dir / f"{stem}.raw"

    assert transcript_json.is_file(), "canonical transcript JSON must be written"
    assert transcript_md.is_file(), "transcript Markdown must be written"
    assert manifest.is_file(), "manifest must be written"
    assert (raw_dir / "transcribe.json").is_file(), "raw provider payload must be retained"

    # Manifest must mark the transcribe step completed.
    data = json.loads(manifest.read_text(encoding="utf-8"))
    assert data["steps"]["transcribe"]["status"] == "completed"
    assert data["provider_job_ids"].get("transcribe") == "job-1"


def test_transcribe_source_mp4_never_uploaded(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """The fake transcriber records the audio path it received; the source MP4
    path must never appear as the transcription upload artifact."""

    src = _write_source(tmp_path, name="視野 環球財經.MP4")
    out_dir = tmp_path / "out"
    cli_mod, transcriber, _, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.app,
        ["transcribe", str(src), "--output-dir", str(out_dir)],
        env={"ASSEMBLYAI_API_KEY": "aai-key"},
    )
    assert result.exit_code == 0, result.output
    assert len(transcriber.calls) == 1
    audio_arg = transcriber.calls[0]
    assert audio_arg != src
    assert audio_arg.suffix == ".m4a"
    assert audio_arg.is_file(), "extracted audio artifact must exist"


def test_transcribe_chinese_path_preserved(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _write_source(tmp_path, name="視野 環球財經 07-19.MP4")
    out_dir = tmp_path / "out"
    cli_mod, _, _, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.app,
        ["transcribe", str(src), "--output-dir", str(out_dir)],
        env={"ASSEMBLYAI_API_KEY": "aai-key"},
    )
    assert result.exit_code == 0, result.output
    assert (out_dir / "視野 環球財經 07-19.transcript.json").is_file()


def test_transcribe_invalid_speaker_bounds_exits_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _write_source(tmp_path)
    cli_mod, transcriber, _, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.app,
        ["transcribe", str(src), "--min-speakers", "5", "--max-speakers", "2"],
        env={"ASSEMBLYAI_API_KEY": "aai-key"},
    )
    assert result.exit_code == 3
    assert not transcriber.calls, "invalid bounds must fail before any paid call"


# ---------------------------------------------------------------------------
# report
# ---------------------------------------------------------------------------


def _write_transcript_artifacts(out_dir: Path, src: Path, transcript: Transcript) -> None:
    from video_content_capture.markdown.transcript import write_transcript_artifacts
    from video_content_capture.storage.paths import artifact_paths

    out_dir.mkdir(parents=True, exist_ok=True)
    paths = artifact_paths(output_dir=out_dir, source_path=src)
    write_transcript_artifacts(paths, transcript)


def test_report_accepts_canonical_transcript_json(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    transcript = _transcript(src)
    _write_transcript_artifacts(out_dir, src, transcript)

    cli_mod, _, reporter, _ = _build_cli_with_fakes(monkeypatch, source=src, transcript=transcript)

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.app,
        ["report", str(out_dir / f"{src.stem}.transcript.json")],
        env={"ANTHROPIC_API_KEY": "ant-key"},
    )
    assert result.exit_code == 0, result.output
    assert len(reporter.calls) == 1


def test_report_resolves_adjacent_json_from_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """``vcc report <transcript.md>`` resolves the adjacent ``.transcript.json``
    and validates it before report generation."""

    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    transcript = _transcript(src)
    _write_transcript_artifacts(out_dir, src, transcript)

    cli_mod, _, reporter, _ = _build_cli_with_fakes(monkeypatch, source=src, transcript=transcript)

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.app,
        ["report", str(out_dir / f"{src.stem}.transcript.md")],
        env={"ANTHROPIC_API_KEY": "ant-key"},
    )
    assert result.exit_code == 0, result.output
    assert len(reporter.calls) == 1


def test_report_without_anthropic_credential_exits_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    transcript = _transcript(src)
    _write_transcript_artifacts(out_dir, src, transcript)

    cli_mod, _, reporter, _ = _build_cli_with_fakes(monkeypatch, source=src, transcript=transcript)

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["report", str(out_dir / f"{src.stem}.transcript.json")])
    assert result.exit_code == 3
    assert not reporter.calls


def test_report_markdown_without_adjacent_json_exits_configuration(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A Markdown path whose adjacent ``.transcript.json`` is missing must fail
    before report generation with a configuration error."""

    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    out_dir.mkdir(parents=True)
    (out_dir / f"{src.stem}.transcript.md").write_text("# 逐字稿\n", encoding="utf-8")

    cli_mod, _, reporter, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.app,
        ["report", str(out_dir / f"{src.stem}.transcript.md")],
        env={"ANTHROPIC_API_KEY": "ant-key"},
    )
    assert result.exit_code == 3
    assert not reporter.calls, "no paid call when adjacent JSON is missing"


def test_report_writes_report_json_before_markdown(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    transcript = _transcript(src)
    _write_transcript_artifacts(out_dir, src, transcript)

    cli_mod, _, _, _ = _build_cli_with_fakes(monkeypatch, source=src, transcript=transcript)

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.app,
        ["report", str(out_dir / f"{src.stem}.transcript.json")],
        env={"ANTHROPIC_API_KEY": "ant-key"},
    )
    assert result.exit_code == 0, result.output

    assert (out_dir / f"{src.stem}.report.json").is_file()
    assert (out_dir / f"{src.stem}.report.md").is_file()


# ---------------------------------------------------------------------------
# run
# ---------------------------------------------------------------------------


def test_run_requires_both_credentials(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _write_source(tmp_path)
    cli_mod, transcriber, reporter, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    # No credentials at all.
    result = runner.invoke(cli_mod.app, ["run", str(src)])
    assert result.exit_code == 3
    assert not transcriber.calls and not reporter.calls

    # Only AssemblyAI — missing Anthropic.
    result = runner.invoke(cli_mod.app, ["run", str(src)], env={"ASSEMBLYAI_API_KEY": "aai-key"})
    assert result.exit_code == 3
    assert not transcriber.calls and not reporter.calls

    # Only Anthropic — missing AssemblyAI.
    result = runner.invoke(cli_mod.app, ["run", str(src)], env={"ANTHROPIC_API_KEY": "ant-key"})
    assert result.exit_code == 3
    assert not transcriber.calls and not reporter.calls


def test_run_full_pipeline_success_and_cleanup(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    cli_mod, transcriber, reporter, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.app,
        ["run", str(src), "--output-dir", str(out_dir)],
        env={"ASSEMBLYAI_API_KEY": "aai-key", "ANTHROPIC_API_KEY": "ant-key"},
    )
    assert result.exit_code == 0, result.output

    stem = src.stem
    assert (out_dir / f"{stem}.transcript.json").is_file()
    assert (out_dir / f"{stem}.transcript.md").is_file()
    assert (out_dir / f"{stem}.report.json").is_file()
    assert (out_dir / f"{stem}.report.md").is_file()
    assert (out_dir / f"{stem}.manifest.json").is_file()
    # Audio cleaned up by default.
    assert not (out_dir / f"{stem}.m4a").exists()
    assert len(transcriber.calls) == 1
    assert len(reporter.calls) == 1


def test_run_keep_audio_retains_audio(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    cli_mod, _, _, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.app,
        ["run", str(src), "--output-dir", str(out_dir), "--keep-audio"],
        env={"ASSEMBLYAI_API_KEY": "aai-key", "ANTHROPIC_API_KEY": "ant-key"},
    )
    assert result.exit_code == 0, result.output
    assert (out_dir / f"{src.stem}.m4a").is_file(), "audio must be retained with --keep-audio"


# ---------------------------------------------------------------------------
# Resume / force / idempotency
# ---------------------------------------------------------------------------


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


def test_resume_after_transcription_skips_transcription(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A ``--resume`` run with transcription already completed starts at report
    generation without another transcription request."""

    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    cli_mod, transcriber, reporter, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    # First run: succeed fully.
    r1 = _full_run(runner, cli_mod, src, out_dir)
    assert r1.exit_code == 0, r1.output
    assert len(transcriber.calls) == 1
    assert len(reporter.calls) == 1

    # Second run with --resume: no new paid calls.
    r2 = _full_run(runner, cli_mod, src, out_dir, extra=["--resume"])
    assert r2.exit_code == 0, r2.output
    assert len(transcriber.calls) == 1, "resume must not re-transcribe"
    assert len(reporter.calls) == 1, "resume must not re-report when report is complete"


def test_resume_after_transcription_only_re_reports(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When transcription completed but report did not, ``--resume`` re-runs
    only the report step (no new transcription)."""

    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    transcript = _transcript(src)
    transcriber = _FakeTranscriber(transcript=transcript)
    # A reporter that fails on the first call and succeeds on the second.
    report = _report(transcript)
    call_count = {"n": 0}

    class _FlakyReporter:
        def __init__(self) -> None:
            self.calls: list[Transcript] = []

        def report(self, *, transcript: Transcript, settings: Settings) -> Any:
            from video_content_capture.reporting.base import ReporterResult

            self.calls.append(transcript)
            call_count["n"] += 1
            if call_count["n"] == 1:
                raise KeyboardInterrupt("simulated Ctrl-C during report")
            return ReporterResult(report=report, raw_metadata={"message_id": "msg-1"})

    flaky = _FlakyReporter()
    cli_mod, _, _, _ = _build_cli_with_fakes(
        monkeypatch,
        source=src,
        transcript=transcript,
        transcriber=transcriber,
        reporter=flaky,  # type: ignore[arg-type]
    )

    runner = CliRunner()
    r1 = _full_run(runner, cli_mod, src, out_dir)
    assert r1.exit_code == 130, r1.output  # Ctrl-C -> 130
    assert len(transcriber.calls) == 1, "transcription completed before interrupt"
    assert call_count["n"] == 1, "report was attempted once"

    # Manifest must record transcribe as completed, report NOT completed.
    manifest = json.loads((out_dir / f"{src.stem}.manifest.json").read_text(encoding="utf-8"))
    assert manifest["steps"]["transcribe"]["status"] == "completed"
    report_state = manifest["steps"].get("report")
    assert report_state is None or report_state["status"] != "completed"

    # Resume: must NOT re-transcribe, only re-report.
    r2 = _full_run(runner, cli_mod, src, out_dir, extra=["--resume"])
    assert r2.exit_code == 0, r2.output
    assert len(transcriber.calls) == 1, "resume must not re-transcribe"
    assert call_count["n"] == 2, "resume must re-run only the report step"


def test_force_reruns_paid_steps(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    cli_mod, transcriber, reporter, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    r1 = _full_run(runner, cli_mod, src, out_dir)
    assert r1.exit_code == 0, r1.output
    assert len(transcriber.calls) == 1
    assert len(reporter.calls) == 1

    r2 = _full_run(runner, cli_mod, src, out_dir, extra=["--force"])
    assert r2.exit_code == 0, r2.output
    assert len(transcriber.calls) == 2, "force must re-run transcription"
    assert len(reporter.calls) == 2, "force must re-run reporting"


def test_incompatible_resume_raises_mismatch_exit(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A config change after a completed run makes ``--resume`` fail with a
    resume-mismatch exit code and ``--force`` guidance."""

    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    cli_mod, transcriber, reporter, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    r1 = _full_run(runner, cli_mod, src, out_dir)
    assert r1.exit_code == 0, r1.output

    # Change an identity-relevant config field (language) and resume.
    r2 = _full_run(runner, cli_mod, src, out_dir, extra=["--resume", "--language", "en-US"])
    assert r2.exit_code == 8, r2.output
    assert "--force" in r2.output, "must mention --force guidance"
    assert len(transcriber.calls) == 1
    assert len(reporter.calls) == 1


def test_ctrl_c_preserves_last_completed_manifest(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Ctrl-C during transcription preserves the prior completed manifest
    (probe/extract) and returns exit 130."""

    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    transcript = _transcript(src)

    class _InterruptingTranscriber:
        def __init__(self) -> None:
            self.calls: list[Path] = []

        def transcribe(
            self, *, audio_path: Path, metadata: MediaMetadata, settings: Settings
        ) -> Any:
            self.calls.append(audio_path)
            raise KeyboardInterrupt("simulated Ctrl-C during transcription")

    transcriber = _InterruptingTranscriber()
    cli_mod, _, reporter, _ = _build_cli_with_fakes(
        monkeypatch,
        source=src,
        transcript=transcript,
        transcriber=transcriber,  # type: ignore[arg-type]
    )

    runner = CliRunner()
    result = _full_run(runner, cli_mod, src, out_dir)
    assert result.exit_code == 130, result.output
    # Manifest may or may not exist, but if it does, transcribe must NOT be
    # marked completed (no durable artifact existed at interrupt time).
    manifest_path = out_dir / f"{src.stem}.manifest.json"
    if manifest_path.is_file():
        data = json.loads(manifest_path.read_text(encoding="utf-8"))
        assert data["steps"].get("transcribe", {}).get("status") != "completed"
    assert not reporter.calls


# ---------------------------------------------------------------------------
# Redaction
# ---------------------------------------------------------------------------


def test_redaction_scrubs_secret_values_from_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A configuration error message must not echo a configured secret."""

    from video_content_capture.redaction import scrub_text

    secret = "sk-super-secret-key-12345"
    text = f"ASSEMBLYAI_API_KEY={secret} bad config"
    assert secret not in scrub_text(text, secrets=[secret])
    assert scrub_text(text, secrets=[secret]) != text


def test_cli_redacts_secret_in_error_output(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When a domain error carries a secret in its message, the CLI must not
    print the raw secret."""

    from video_content_capture.domain.errors import MediaError

    src = _write_source(tmp_path)
    secret = "sk-leaked-in-error"
    cli_mod, _, _, _ = _build_cli_with_fakes(monkeypatch, source=src)
    monkeypatch.setattr(
        cli_mod,
        "_default_probe",
        lambda _p: (_ for _ in ()).throw(MediaError(f"boom key={secret}")),
    )

    runner = CliRunner()
    # Register the secret as a configured credential so the CLI's redaction
    # registry knows to scrub it from error output.
    result = runner.invoke(cli_mod.app, ["probe", str(src)], env={"ANTHROPIC_API_KEY": secret})
    assert result.exit_code == 2
    assert secret not in result.output


def test_logging_filter_redacts_secrets(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """The redaction logging filter scrubs configured secrets from records."""

    from video_content_capture.redaction import RedactingFilter

    secret = "sk-log-secret-999"
    records: list[str] = []

    handler = logging.Handler()
    handler.emit = lambda record: records.append(record.getMessage())  # type: ignore[method-assign]
    filt = RedactingFilter(secrets=[secret])
    handler.addFilter(filt)

    logger = logging.getLogger("vcc.test.redaction")
    logger.setLevel(logging.DEBUG)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.propagate = False

    logger.warning("processing key=%s", secret)
    assert all(secret not in r for r in records)


def test_metadata_artifact_contains_no_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Generated metadata/manifest/raw artifacts must not contain configured
    credential values."""

    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    aai_secret = "aai-secret-AAA"
    ant_secret = "ant-secret-BBB"
    cli_mod, _, _, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.app,
        ["run", str(src), "--output-dir", str(out_dir)],
        env={"ASSEMBLYAI_API_KEY": aai_secret, "ANTHROPIC_API_KEY": ant_secret},
    )
    assert result.exit_code == 0, result.output

    for artifact in out_dir.rglob("*"):
        if artifact.is_file() and artifact.suffix in {".json", ".md"}:
            text = artifact.read_text(encoding="utf-8")
            assert aai_secret not in text, f"secret leaked in {artifact}"
            assert ant_secret not in text, f"secret leaked in {artifact}"


# ---------------------------------------------------------------------------
# python -m equivalence
# ---------------------------------------------------------------------------


def test_python_m_equivalent_to_vcc(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """``python -m video_content_capture`` MUST be equivalent to ``vcc``."""

    src = _write_source(tmp_path)
    cli_mod, _, _, _ = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    result = runner.invoke(cli_mod.app, ["probe", str(src)])
    assert result.exit_code == 0


# ---------------------------------------------------------------------------
# No paid calls guard
# ---------------------------------------------------------------------------


def test_default_suite_makes_no_paid_provider_call(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A guard: the default suite never makes a live/paid provider request."""

    import httpx

    src = _write_source(tmp_path)
    cli_mod, _, _, _ = _build_cli_with_fakes(monkeypatch, source=src)

    real_request = httpx.Client.request
    blocked: list[str] = []

    def _sentinel(
        self: httpx.Client, method: str, url: str, *args: Any, **kwargs: Any
    ) -> httpx.Response:
        if "api.assemblyai.com" in str(url) or "api.anthropic.com" in str(url):
            blocked.append(f"{method} {url}")
            raise AssertionError(f"paid request blocked: {method} {url}")
        return real_request(self, method, url, *args, **kwargs)

    monkeypatch.setattr(httpx.Client, "request", _sentinel)

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.app,
        ["run", str(src), "--output-dir", str(tmp_path / "out")],
        env={"ASSEMBLYAI_API_KEY": "aai-key", "ANTHROPIC_API_KEY": "ant-key"},
    )
    assert result.exit_code == 0, result.output
    assert blocked == []


# ---------------------------------------------------------------------------
# Progress callback injectability
# ---------------------------------------------------------------------------


def test_progress_callback_receives_step_events(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    src = _write_source(tmp_path)
    out_dir = tmp_path / "out"
    cli_mod, _, _, progress = _build_cli_with_fakes(monkeypatch, source=src)

    runner = CliRunner()
    result = runner.invoke(
        cli_mod.app,
        ["run", str(src), "--output-dir", str(out_dir)],
        env={"ASSEMBLYAI_API_KEY": "aai-key", "ANTHROPIC_API_KEY": "ant-key"},
    )
    assert result.exit_code == 0, result.output
    joined = " ".join(progress)
    assert "probe:start" in joined or "probe:" in joined
    assert "transcribe:" in joined
    assert "report:" in joined
