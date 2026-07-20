"""Typer CLI entry point for the ``vcc`` console script.

Commands: ``probe``, ``transcribe``, ``report``, ``run``.

Architecture:

* The core :class:`video_content_capture.pipeline.Pipeline` contains no
  Typer/Rich/terminal code. This module is the ONLY place that translates
  typed :class:`DomainError` subclasses into stable exit codes via
  :mod:`video_content_capture.exit_codes`.
* Rich progress is adapted through an injectable progress callback. The
  default production callback renders Rich progress; tests inject a
  collecting callback by monkeypatching the module-level seams
  (``_default_probe``, ``_default_extract``, ``_default_transcriber_factory``,
  ``_default_reporter_factory``, ``_default_progress``).
* Default production wiring constructs cloud clients (AssemblyAI, Anthropic)
  ONLY after the corresponding command's credential validation passes —
  matching the spec's "fail before upload" contract. The factories are
  imported lazily inside the command body so a ``probe`` invocation never
  imports a provider SDK.
* Every configured credential value is redacted from CLI output, Rich
  progress, logging, exception text/details, and generated metadata via
  :mod:`video_content_capture.redaction`.

``python -m video_content_capture`` remains equivalent to ``vcc`` via
:mod:`video_content_capture.__main__`.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

import typer
from pydantic import ValidationError

from video_content_capture.config import Command, ConfigError, Settings
from video_content_capture.domain.errors import DomainError, ResumeMismatchError
from video_content_capture.domain.models import MediaMetadata
from video_content_capture.exit_codes import ExitCode, exit_code_for_category
from video_content_capture.redaction import (
    RedactingFilter,
    register_secrets,
    scrub_exception,
)

app = typer.Typer(
    name="vcc",
    help="Probe media, transcribe speech, and generate grounded reports.",
    no_args_is_help=True,
)


# ---------------------------------------------------------------------------
# Injectable seams (tests monkeypatch these to avoid paid calls)
# ---------------------------------------------------------------------------


def _default_probe(source_path: Path) -> MediaMetadata:
    """Production probe: ffprobe-based, credential-free."""

    from video_content_capture.media.probe import probe_video

    return probe_video(source_path)


def _default_extract(metadata: MediaMetadata, output_path: Path) -> Path:
    """Production audio extraction via ffmpeg."""

    from video_content_capture.media.audio import extract_audio

    return extract_audio(metadata, output_path)


def _default_transcriber_factory(settings: Settings) -> Any:
    """Select the configured production transcription adapter.

    The local MLX and AssemblyAI paths are exclusive; this factory never
    silently falls back between them.
    """

    if settings.transcription_backend == "mlx":
        from video_content_capture.transcription.mlx import MLXWhisperTranscriber

        return MLXWhisperTranscriber()

    from video_content_capture.transcription.assemblyai import AssemblyAITranscriber

    return AssemblyAITranscriber(settings=settings)


def _default_reporter_factory(settings: Settings) -> Any:
    """Production reporter: Claude (Anthropic) adapter.

    Constructed AFTER credential validation.
    """

    from video_content_capture.reporting.claude import ClaudeReporter

    return ClaudeReporter(settings=settings)


def _default_progress(step: str, event: str, **data: Any) -> None:
    """Default progress callback: render Rich progress.

    Kept minimal and deterministic. Rich is imported lazily so the pipeline
    module never depends on it.
    """

    # Render a single quiet line per event. Rich rendering is intentionally
    # minimal here; the injectable callback is the contract, not the visual
    # presentation.
    try:
        from rich.console import Console

        console = Console(stderr=True)
        console.print(f"[dim]{step} {event}[/dim]")
    except Exception:  # pragma: no cover — Rich is a hard dep, but never fail
        # the run on rendering.
        print(f"{step} {event}", file=sys.stderr)  # noqa: T201


# ---------------------------------------------------------------------------
# Verbosity / logging
# ---------------------------------------------------------------------------


def _configure_logging(verbosity: int) -> None:
    """Configure the ``vcc`` logger with the redacting filter installed."""

    level = logging.WARNING
    if verbosity >= 2:
        level = logging.DEBUG
    elif verbosity == 1:
        level = logging.INFO

    logger = logging.getLogger("vcc")
    logger.setLevel(level)
    # Replace handlers to avoid duplicate output across repeated invocations
    # in the same process (tests).
    logger.handlers.clear()
    handler = logging.StreamHandler()
    handler.setLevel(level)
    handler.addFilter(RedactingFilter())
    logger.addHandler(handler)
    logger.propagate = False


def _register_secrets_from_settings(settings: Settings) -> None:
    """Register configured secret values for process-wide redaction."""

    secrets: list[str] = []
    if settings.assemblyai_api_key is not None:
        value = settings.assemblyai_api_key.get_secret_value()
        if value:
            secrets.append(value)
    if settings.anthropic_api_key is not None:
        value = settings.anthropic_api_key.get_secret_value()
        if value:
            secrets.append(value)
    register_secrets(secrets)


# ---------------------------------------------------------------------------
# Shared option resolution
# ---------------------------------------------------------------------------


def _build_settings(
    *,
    output_dir: Path | None,
    language: str | None,
    transcription_backend: str | None,
    min_speakers: int | None,
    max_speakers: int | None,
    keep_audio: bool,
) -> Settings:
    """Build a :class:`Settings` instance from CLI options + environment.

    Precedence: explicit CLI option > environment variable > default. The
    :class:`Settings` model itself encodes env-var defaults, so we only pass
    explicit overrides.
    """

    overrides: dict[str, Any] = {}
    if output_dir is not None:
        overrides["output_dir"] = output_dir
    if language is not None:
        overrides["language"] = language
    if transcription_backend is not None:
        overrides["transcription_backend"] = transcription_backend
    if min_speakers is not None:
        overrides["min_speakers"] = min_speakers
    if max_speakers is not None:
        overrides["max_speakers"] = max_speakers
    overrides["keep_audio"] = keep_audio
    try:
        return Settings(**overrides)
    except (ConfigError, ValidationError) as exc:
        # Surface as a configuration error at the CLI boundary.
        raise typer.Exit(code=int(ExitCode.CONFIGURATION)) from exc


def _default_output_dir(settings: Settings, source: Path) -> Path:
    """Resolve the effective output directory."""

    if settings.output_dir is not None:
        return settings.output_dir
    # Default: alongside the source file.
    return source.parent


# ---------------------------------------------------------------------------
# Error boundary
# ---------------------------------------------------------------------------


def _handle_domain_error(exc: DomainError) -> int:
    """Translate a typed domain error into a documented exit code and print a
    redacted message. Returns the exit code.

    For :class:`FilesystemError` (completed-step corruption detected on
    resume) and :class:`ResumeMismatchError`, append explicit ``--force``
    guidance so the operator knows how to start a fresh attempt rather than
    silently re-running paid work.
    """

    from video_content_capture.domain.errors import FilesystemError, ResumeMismatchError

    code = exit_code_for_category(exc.category) if exc.category is not None else ExitCode.UNEXPECTED
    redacted = scrub_exception(exc)
    guidance = ""
    if isinstance(exc, (FilesystemError, ResumeMismatchError)):
        guidance = " -- use --force to start a new attempt."
    typer.echo(f"error: {redacted}{guidance}", err=True)
    return int(code)


def _handle_keyboard_interrupt() -> int:
    """Ctrl-C: preserve the last completed manifest (the pipeline already
    writes manifests atomically and never marks a step completed before its
    durable artifacts exist). Return the conventional 130 exit code."""

    typer.echo("interrupted: state preserved for --resume", err=True)
    return int(ExitCode.INTERRUPTED)


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@app.command()
def probe(
    video: Path = typer.Argument(..., help="Path to the source video file."),
    output_dir: Path | None = typer.Option(
        None, "--output-dir", help="Output directory (default: alongside source)."
    ),
    verbose: int = typer.Option(0, "-v", "--verbose", count=True, help="Increase verbosity."),
) -> None:
    """Probe media metadata without cloud credentials."""

    _configure_logging(verbose)
    settings = _build_settings(
        output_dir=output_dir,
        language=None,
        transcription_backend=None,
        min_speakers=None,
        max_speakers=None,
        keep_audio=False,
    )
    _register_secrets_from_settings(settings)

    from video_content_capture.pipeline import Pipeline

    pipeline = Pipeline(
        probe_fn=_default_probe,
        extract_fn=_default_extract,
        transcriber_factory=_default_transcriber_factory,
        reporter_factory=_default_reporter_factory,
        progress=_default_progress,
    )
    try:
        result = pipeline.probe(source_path=video)
    except DomainError as exc:
        raise typer.Exit(code=_handle_domain_error(exc)) from exc
    except KeyboardInterrupt:
        raise typer.Exit(code=_handle_keyboard_interrupt()) from KeyboardInterrupt

    metadata = result.metadata
    duration_seconds = metadata.duration_seconds
    minutes, seconds = divmod(int(duration_seconds), 60)
    hours, minutes = divmod(minutes, 60)
    duration_hms = f"{hours:02d}:{minutes:02d}:{seconds:02d}"

    typer.echo(f"file: {video}")
    typer.echo(f"container: {metadata.container}")
    typer.echo(f"duration: {duration_hms} ({duration_seconds:.3f}s)")
    typer.echo(f"video streams: {len(metadata.video_streams)}")
    typer.echo(f"audio streams: {len(metadata.audio_streams)}")
    typer.echo(f"subtitle streams: {len(metadata.subtitle_streams)}")
    for stream in metadata.audio_streams:
        typer.echo(
            f"  audio[{stream.index}] codec={stream.codec} "
            f"channels={stream.channels} sample_rate={stream.sample_rate}"
        )
    for stream in metadata.video_streams:
        typer.echo(
            f"  video[{stream.index}] codec={stream.codec} "
            f"width={stream.width} height={stream.height}"
        )


@app.command()
def transcribe(
    video: Path = typer.Argument(..., help="Path to the source video file."),
    output_dir: Path | None = typer.Option(
        None, "--output-dir", help="Output directory (default: alongside source)."
    ),
    language: str | None = typer.Option(None, "--language", help="Language code (default: zh-TW)."),
    transcription_backend: str | None = typer.Option(
        None,
        "--transcription-backend",
        help="Transcription backend: assemblyai (default) or mlx (local Apple Silicon).",
    ),
    min_speakers: int | None = typer.Option(
        None, "--min-speakers", help="Minimum number of speakers."
    ),
    max_speakers: int | None = typer.Option(
        None, "--max-speakers", help="Maximum number of speakers."
    ),
    resume: bool = typer.Option(False, "--resume/--no-resume", help="Resume from prior state."),
    force: bool = typer.Option(False, "--force", help="Start a fresh attempt."),
    keep_audio: bool = typer.Option(False, "--keep-audio", help="Retain extracted audio."),
    verbose: int = typer.Option(0, "-v", "--verbose", count=True, help="Increase verbosity."),
) -> None:
    """Transcribe speech; diarization depends on the selected backend."""

    _run_cloud_command(
        command=Command.TRANSCRIBE,
        video=video,
        output_dir=output_dir,
        language=language,
        transcription_backend=transcription_backend,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
        resume=resume,
        force=force,
        keep_audio=keep_audio,
        verbose=verbose,
    )


@app.command()
def report(
    transcript: Path = typer.Argument(
        ..., help="Path to a canonical .transcript.json or generated .transcript.md."
    ),
    output_dir: Path | None = typer.Option(
        None, "--output-dir", help="Output directory (default: transcript directory)."
    ),
    language: str | None = typer.Option(None, "--language", help="Language code (default: zh-TW)."),
    min_speakers: int | None = typer.Option(
        None, "--min-speakers", help="Minimum number of speakers."
    ),
    max_speakers: int | None = typer.Option(
        None, "--max-speakers", help="Maximum number of speakers."
    ),
    resume: bool = typer.Option(False, "--resume/--no-resume", help="Resume from prior state."),
    force: bool = typer.Option(False, "--force", help="Start a fresh attempt."),
    verbose: int = typer.Option(0, "-v", "--verbose", count=True, help="Increase verbosity."),
) -> None:
    """Generate a grounded report from a transcript."""

    _run_report_command(
        transcript=transcript,
        output_dir=output_dir,
        language=language,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
        resume=resume,
        force=force,
        verbose=verbose,
    )


@app.command()
def run(
    video: Path = typer.Argument(..., help="Path to the source video file."),
    output_dir: Path | None = typer.Option(
        None, "--output-dir", help="Output directory (default: alongside source)."
    ),
    language: str | None = typer.Option(None, "--language", help="Language code (default: zh-TW)."),
    transcription_backend: str | None = typer.Option(
        None,
        "--transcription-backend",
        help="Transcription backend: assemblyai (default) or mlx (local Apple Silicon).",
    ),
    min_speakers: int | None = typer.Option(
        None, "--min-speakers", help="Minimum number of speakers."
    ),
    max_speakers: int | None = typer.Option(
        None, "--max-speakers", help="Maximum number of speakers."
    ),
    resume: bool = typer.Option(False, "--resume/--no-resume", help="Resume from prior state."),
    force: bool = typer.Option(False, "--force", help="Start a fresh attempt."),
    keep_audio: bool = typer.Option(False, "--keep-audio", help="Retain extracted audio."),
    verbose: int = typer.Option(0, "-v", "--verbose", count=True, help="Increase verbosity."),
) -> None:
    """Run the complete pipeline."""

    _run_cloud_command(
        command=Command.RUN,
        video=video,
        output_dir=output_dir,
        language=language,
        transcription_backend=transcription_backend,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
        resume=resume,
        force=force,
        keep_audio=keep_audio,
        verbose=verbose,
    )


# ---------------------------------------------------------------------------
# Command dispatch helpers
# ---------------------------------------------------------------------------


def _run_cloud_command(
    *,
    command: Command,
    video: Path,
    output_dir: Path | None,
    language: str | None,
    transcription_backend: str | None,
    min_speakers: int | None,
    max_speakers: int | None,
    resume: bool,
    force: bool,
    keep_audio: bool,
    verbose: int,
) -> None:
    """Shared dispatch for the ``transcribe`` and ``run`` commands."""

    _configure_logging(verbose)
    settings = _build_settings(
        output_dir=output_dir,
        language=language,
        transcription_backend=transcription_backend,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
        keep_audio=keep_audio,
    )
    _register_secrets_from_settings(settings)

    # Credential validation happens inside the pipeline (via
    # validate_for_command) BEFORE any provider client is constructed. But we
    # also validate here so the CLI fails fast with the documented exit code
    # and a redacted message without entering the pipeline.
    try:
        settings.validate_for_command(command)
    except ConfigError as exc:
        typer.echo(f"error: {scrub_exception(exc)}", err=True)
        raise typer.Exit(code=int(ExitCode.CONFIGURATION)) from exc

    from video_content_capture.pipeline import Pipeline

    pipeline = Pipeline(
        probe_fn=_default_probe,
        extract_fn=_default_extract,
        transcriber_factory=_default_transcriber_factory,
        reporter_factory=_default_reporter_factory,
        progress=_default_progress,
    )

    effective_output_dir = _default_output_dir(settings, video)

    try:
        if command is Command.TRANSCRIBE:
            tresult = pipeline.transcribe(
                source_path=video,
                settings=settings,
                output_dir=effective_output_dir,
                resume=resume,
                force=force,
            )
            typer.echo(f"transcript: {tresult.artifact_paths.transcript_json}")
            typer.echo(f"transcript markdown: {tresult.artifact_paths.transcript_md}")
        else:  # Command.RUN
            rresult = pipeline.run(
                source_path=video,
                settings=settings,
                output_dir=effective_output_dir,
                resume=resume,
                force=force,
            )
            typer.echo(f"transcript: {rresult.artifact_paths.transcript_json}")
            typer.echo(f"transcript markdown: {rresult.artifact_paths.transcript_md}")
            typer.echo(f"report: {rresult.artifact_paths.report_json}")
            typer.echo(f"report markdown: {rresult.artifact_paths.report_md}")
    except ResumeMismatchError as exc:
        # Resume mismatch: include --force guidance and the documented exit.
        redacted = scrub_exception(exc)
        typer.echo(
            f"error: {redacted} -- use --force to start a new attempt.",
            err=True,
        )
        raise typer.Exit(code=int(ExitCode.RESUME_MISMATCH)) from exc
    except DomainError as exc:
        raise typer.Exit(code=_handle_domain_error(exc)) from exc
    except KeyboardInterrupt:
        raise typer.Exit(code=_handle_keyboard_interrupt()) from KeyboardInterrupt


def _run_report_command(
    *,
    transcript: Path,
    output_dir: Path | None,
    language: str | None,
    min_speakers: int | None,
    max_speakers: int | None,
    resume: bool,
    force: bool,
    verbose: int,
) -> None:
    """Dispatch for the ``report`` command (validates Anthropic only)."""

    _configure_logging(verbose)
    settings = _build_settings(
        output_dir=output_dir,
        language=language,
        transcription_backend=None,
        min_speakers=min_speakers,
        max_speakers=max_speakers,
        keep_audio=False,
    )
    _register_secrets_from_settings(settings)

    try:
        settings.validate_for_command(Command.REPORT)
    except ConfigError as exc:
        typer.echo(f"error: {scrub_exception(exc)}", err=True)
        raise typer.Exit(code=int(ExitCode.CONFIGURATION)) from exc

    from video_content_capture.pipeline import Pipeline

    pipeline = Pipeline(
        probe_fn=_default_probe,
        extract_fn=_default_extract,
        transcriber_factory=_default_transcriber_factory,
        reporter_factory=_default_reporter_factory,
        progress=_default_progress,
    )

    try:
        result = pipeline.report(
            transcript_path=transcript,
            settings=settings,
            output_dir=output_dir,
            resume=resume,
            force=force,
        )
        typer.echo(f"report: {result.artifact_paths.report_json}")
        typer.echo(f"report markdown: {result.artifact_paths.report_md}")
    except ResumeMismatchError as exc:
        redacted = scrub_exception(exc)
        typer.echo(
            f"error: {redacted} -- use --force to start a new attempt.",
            err=True,
        )
        raise typer.Exit(code=int(ExitCode.RESUME_MISMATCH)) from exc
    except DomainError as exc:
        raise typer.Exit(code=_handle_domain_error(exc)) from exc
    except KeyboardInterrupt:
        raise typer.Exit(code=_handle_keyboard_interrupt()) from KeyboardInterrupt


# A type alias kept for clarity in factory signatures.
ProgressCallback = Callable[..., None]


if __name__ == "__main__":  # pragma: no cover
    app()
