"""Core pipeline use cases: probe, transcribe, report, run.

This module coordinates the accepted media / storage / transcription /
reporting / rendering services into provider-neutral use cases. It contains
NO Typer, NO Rich, and NO terminal printing — terminal presentation is owned
by :mod:`video_content_capture.cli`. Progress is reported through an
injectable :data:`Progress` callback carrying stable step/event data, so
tests use a collecting callback instead of terminal rendering.

Design contracts (binding requirements):

* Dependency injection for probe / extract / transcriber factory / reporter
  factory / progress / sleeper / source-hash / config-hash. Tests inject
  fakes so the default suite never makes a paid API request.
* The source MP4 is never uploaded: the extracted ``.m4a`` artifact path is
  the only path passed to the transcriber.
* Canonical transcript JSON is written BEFORE transcript Markdown; canonical
  report JSON is written BEFORE report Markdown (accepted renderers enforce
  this and we delegate to them).
* The manifest is atomically advanced ONLY after each artifact set is valid.
  Provider job IDs and artifact checksums are recorded.
* Resume: a paid step (transcribe, report) is skipped only when its manifest
  entry is COMPLETED, its artifact exists, and its checksum matches. Resume
  after transcription starts at report generation without another
  transcription request. A second compatible resume makes no transcription
  OR reporting request.
* ``--force`` discards prior state and re-runs paid steps.
* Incompatible resume raises :class:`ResumeMismatchError` with ``--force``
  guidance.
* ``KeyboardInterrupt`` (Ctrl-C) preserves the last completed manifest and
  propagates so the CLI can exit 130; no step is marked completed before its
  durable artifacts exist.
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC
from pathlib import Path
from typing import Any, Protocol

from video_content_capture.config import Command, Settings
from video_content_capture.domain.errors import (
    ConfigurationError,
    FilesystemError,
    ResumeMismatchError,
)
from video_content_capture.domain.models import (
    MediaMetadata,
    ProcessingStepState,
    Report,
    RunMetadata,
    StepStatus,
    Transcript,
)
from video_content_capture.markdown.report import write_report_artifacts
from video_content_capture.markdown.transcript import write_transcript_artifacts
from video_content_capture.reporting.base import Reporter, ReporterResult
from video_content_capture.storage.artifacts import (
    cleanup_audio,
    compute_checksum,
    validate_artifact_checksum,
    validate_completed_step,
    write_metadata,
    write_raw_provider_payload,
)
from video_content_capture.storage.manifest import (
    Manifest,
    load_resume_state,
    write_manifest,
)
from video_content_capture.storage.paths import (
    artifact_paths,
    compute_config_hash,
    compute_source_hash,
)
from video_content_capture.transcription.base import Transcriber, TranscriptionResult

# Step names used in the manifest and progress events. Stable.
STEP_PROBE = "probe"
STEP_EXTRACT = "extract"
STEP_TRANSCRIBE = "transcribe"
STEP_REPORT = "report"
STEP_CLEANUP = "cleanup"

# Paid steps that resume may skip after checksum/existence validation.
_PAID_STEPS = (STEP_TRANSCRIBE, STEP_REPORT)


# --- Progress protocol ----------------------------------------------------


class Progress(Protocol):
    """Injectable progress callback carrying stable step/event data.

    The CLI adapts this to Rich; tests use a collecting callback. The
    callback receives the step name, an event string (``start`` / ``completed``
    / ``skipped`` / ``failed``), and arbitrary keyword data (e.g.
    ``artifact=...``, ``provider_job_id=...``). Implementations MUST NOT
    include credential values in the data.
    """

    def __call__(self, step: str, event: str, **data: Any) -> None: ...


#: Injectable probe function: given a source path, returns MediaMetadata.
ProbeFn = Callable[[Path], MediaMetadata]

#: Injectable extractor: given metadata + output path, returns the audio path.
ExtractFn = Callable[[MediaMetadata, Path], Path]

#: Injectable transcriber factory: builds a Transcriber from Settings.
TranscriberFactory = Callable[[Settings], Transcriber]

#: Injectable reporter factory: builds a Reporter from Settings.
ReporterFactory = Callable[[Settings], Reporter]

#: Injectable sleeper for retry backoff (tests inject no-op).
Sleeper = Callable[[float], None]

#: Injectable source hash function (tests may inject a fast stub).
SourceHasher = Callable[[Path], str]

#: Injectable config hash function (tests may inject a fast stub).
ConfigHasher = Callable[[Settings], str]


def _real_sleep(seconds: float) -> None:
    """Default sleeper that actually sleeps. Tests inject a no-op."""

    import time

    time.sleep(seconds)


# --- Result dataclasses ---------------------------------------------------


@dataclass
class ProbeResult:
    """Result of the ``probe`` use case."""

    metadata: MediaMetadata


@dataclass
class TranscribeResult:
    """Result of the ``transcribe`` use case.

    ``transcribed`` is True when a transcription was actually performed this
    run; False when a compatible resume skipped the paid step.
    """

    transcript: Transcript
    transcribed: bool
    artifact_paths: Any  # ArtifactPaths


@dataclass
class ReportResult:
    """Result of the ``report`` use case.

    ``reported`` is True when a report was actually generated this run; False
    when a compatible resume skipped the paid step.
    """

    report: Report
    reported: bool
    artifact_paths: Any  # ArtifactPaths


@dataclass
class RunResult:
    """Result of the ``run`` use case."""

    transcript: Transcript
    report: Report
    transcribed: bool
    reported: bool
    artifact_paths: Any  # ArtifactPaths


# --- Pipeline -------------------------------------------------------------


class Pipeline:
    """Coordinates probe / extract / transcribe / report / cleanup.

    All collaborators are injected so tests never make a paid call. The
    pipeline raises typed :class:`DomainError` subclasses; the CLI boundary
    converts them to exit codes.
    """

    def __init__(
        self,
        *,
        probe_fn: ProbeFn,
        extract_fn: ExtractFn,
        transcriber_factory: TranscriberFactory,
        reporter_factory: ReporterFactory,
        progress: Progress,
        sleeper: Sleeper | None = None,
        source_hasher: SourceHasher | None = None,
        config_hasher: ConfigHasher | None = None,
    ) -> None:
        self._probe = probe_fn
        self._extract = extract_fn
        self._transcriber_factory = transcriber_factory
        self._reporter_factory = reporter_factory
        self._progress = progress
        self._sleeper = sleeper or _real_sleep
        self._source_hasher = source_hasher or compute_source_hash
        self._config_hasher = config_hasher or compute_config_hash

    # --- Shared setup ----------------------------------------------------

    def _resolve_settings(
        self,
        settings: Settings,
        *,
        command: Command,
        output_dir: Path,
        source_path: Path,
        resume: bool,
        force: bool,
    ) -> tuple[Any, str, str, str, Manifest | None]:
        """Validate credentials/bounds, compute hashes, paths, and resume state.

        Returns ``(paths, content_hash, config_hash, attempt_id, prior_manifest)``.
        ``prior_manifest`` is ``None`` when there is no compatible resume
        state (and ``force=True`` always yields ``None``). Raises
        :class:`ConfigurationError` for missing credentials or invalid
        bounds BEFORE any provider client is constructed, and
        :class:`ResumeMismatchError` for incompatible resume state.
        """

        settings.validate_for_command(command)
        paths = artifact_paths(output_dir=output_dir, source_path=source_path)
        content_hash = self._source_hasher(source_path)
        config_hash = self._config_hasher(settings)
        attempt_id = uuid.uuid4().hex
        prior = load_resume_state(
            paths.manifest,
            content_hash=content_hash,
            config_hash=config_hash,
            force=force,
        )
        if resume and not force and prior is None and paths.manifest.is_file():
            raise FilesystemError(
                "Stored manifest exists but is unreadable; refusing to repeat paid "
                "work during --resume. Repair it or use --force for a new attempt.",
                details={"path": str(paths.manifest)},
            )
        return paths, content_hash, config_hash, attempt_id, prior

    def _fresh_manifest(
        self, *, source_path: Path, content_hash: str, config_hash: str, attempt_id: str
    ) -> Manifest:
        run = RunMetadata(
            source_path=str(source_path),
            content_hash=content_hash,
            config_hash=config_hash,
            attempt_id=attempt_id,
            started_at=_now_iso(),
        )
        return Manifest(
            run_metadata=run,
            content_hash=content_hash,
            config_hash=config_hash,
            attempt_id=attempt_id,
            steps={},
            provider_job_ids={},
            artifact_checksums={},
        )

    def _advance(
        self,
        manifest: Manifest,
        *,
        step: str,
        artifact_path: Path | None,
        provider_job_id: str | None = None,
        extra_steps: dict[str, ProcessingStepState] | None = None,
        required_artifacts: tuple[Path, ...] = (),
    ) -> Manifest:
        """Return a new manifest with ``step`` marked completed and persisted.

        Atomic: the caller writes the new manifest via :func:`write_manifest`
        only after the step's durable artifacts exist.

        Checksums are recorded for:

        * the primary ``artifact_path`` (the canonical JSON used to load the
          model on resume), and
        * every path in ``required_artifacts`` (the full set of durable
          artifacts that MUST exist and match for the step to be trusted on
          resume — at minimum the canonical JSON, the Markdown rendering, and
          the raw provider response).

        Recording checksums for ALL required artifacts means a tampered or
        missing Markdown/raw on a completed step is detected at resume time
        and surfaced as a typed :class:`FilesystemError` (with ``--force``
        guidance at the CLI boundary) rather than silently re-running a paid
        step. The primary ``artifact_path`` is also the value stored on the
        :class:`ProcessingStepState` so existing loaders can resolve the
        canonical JSON.
        """

        steps = dict(manifest.steps)
        # Carry any auxiliary steps (e.g. extract) the caller recorded.
        if extra_steps:
            steps.update(extra_steps)
        primary_artifact = artifact_path.resolve() if artifact_path is not None else None
        state = ProcessingStepState(
            step_name=step,
            status=StepStatus.COMPLETED,
            completed_at=_now_iso(),
            artifact_path=str(primary_artifact) if primary_artifact else None,
        )
        steps[step] = state
        new_checksums = dict(manifest.artifact_checksums)
        # Record checksums for every required durable artifact. The primary
        # artifact_path is included in the required set so callers do not need
        # to list it twice.
        all_required: list[Path] = []
        if primary_artifact is not None:
            all_required.append(primary_artifact)
        all_required.extend(path.resolve() for path in required_artifacts)
        for path in all_required:
            try:
                new_checksums[str(path)] = compute_checksum(path)
            except OSError as exc:
                raise FilesystemError(
                    f"failed to checksum required artifact before completing {step!r}: {path}",
                    details={"step": step, "path": str(path)},
                ) from exc
        provider_job_ids = dict(manifest.provider_job_ids)
        if provider_job_id is not None:
            provider_job_ids[step] = provider_job_id
        return Manifest(
            run_metadata=manifest.run_metadata,
            content_hash=manifest.content_hash,
            config_hash=manifest.config_hash,
            attempt_id=manifest.attempt_id,
            steps=steps,
            provider_job_ids=provider_job_ids,
            artifact_checksums=new_checksums,
        )

    def _step_completed(self, manifest: Manifest | None, step: str) -> bool:
        """True when ``step`` is COMPLETED in ``manifest``."""

        if manifest is None:
            return False
        state = manifest.steps.get(step)
        return state is not None and state.status is StepStatus.COMPLETED

    def _validate_resume_step(
        self, manifest: Manifest | None, step: str, *, base_dir: Path
    ) -> bool:
        """Validate a completed resume step's artifacts before trusting it.

        Returns True when the step is COMPLETED AND every required durable
        artifact exists AND its checksum matches the recorded value. Returns
        False (no error) when the step is NOT completed — the caller simply
        runs the paid step fresh.

        Raises :class:`FilesystemError` (with ``--force`` guidance at the CLI
        boundary) when a step CLAIMS completed but ANY recorded artifact is
        missing or checksum-mismatched. This is the integrity contract: a
        completed step's durable artifacts MUST be intact to be trusted. We
        do NOT silently re-run a paid step on corruption — that could mask
        silent data loss. The operator must either repair the artifact or
        start a fresh attempt with ``--force``.

        Every checksum recorded for the step's required artifact set is
        validated (not just the primary artifact_path), so a tampered
        Markdown or missing raw provider payload on a completed step is
        detected and surfaced.
        """

        if manifest is None or not self._step_completed(manifest, step):
            return False
        state = manifest.steps[step]
        primary_key = state.artifact_path or ""
        primary_expected = manifest.artifact_checksums.get(primary_key)
        if primary_expected is None:
            raise FilesystemError(
                f"completed step {step!r} has no recorded checksum for its primary artifact",
                details={"step": step, "path": primary_key},
            )
        validate_completed_step(state, base_dir=base_dir, expected_checksum=primary_expected)

        for required_key in _required_artifact_keys(primary_key, step):
            expected = manifest.artifact_checksums.get(required_key)
            if expected is None:
                raise FilesystemError(
                    f"completed step {step!r} has no recorded checksum for required artifact",
                    details={"step": step, "path": required_key},
                )
            artifact = Path(required_key)
            if not artifact.is_absolute() and not artifact.is_file():
                artifact = base_dir / artifact
            validate_artifact_checksum(artifact, expected)
        return True

    # --- probe -----------------------------------------------------------

    def probe(self, *, source_path: Path) -> ProbeResult:
        """Probe media metadata without cloud credentials."""

        self._progress(STEP_PROBE, "start")
        metadata = self._probe(source_path)
        self._progress(STEP_PROBE, "completed", container=metadata.container)
        return ProbeResult(metadata=metadata)

    # --- transcribe ------------------------------------------------------

    def transcribe(
        self,
        *,
        source_path: Path,
        settings: Settings,
        output_dir: Path,
        resume: bool = False,
        force: bool = False,
    ) -> TranscribeResult:
        """Probe, extract, transcribe, and write transcript artifacts.

        Validates the AssemblyAI credential (via ``validate_for_command``)
        before constructing any provider client. Writes raw provider payload,
        canonical transcript JSON, then transcript Markdown, and atomically
        advances the manifest.
        """

        paths, content_hash, config_hash, attempt_id, prior = self._resolve_settings(
            settings,
            command=Command.TRANSCRIBE,
            output_dir=output_dir,
            source_path=source_path,
            resume=resume,
            force=force,
        )
        manifest = (
            prior
            if (resume and prior is not None)
            else self._fresh_manifest(
                source_path=source_path,
                content_hash=content_hash,
                config_hash=config_hash,
                attempt_id=attempt_id,
            )
        )

        # 1. Probe (always re-run; credential-free and cheap).
        self._progress(STEP_PROBE, "start")
        metadata = self._probe(source_path)
        self._progress(STEP_PROBE, "completed", container=metadata.container)

        # 2. Extract audio (always re-run; cheap, local). The extracted audio
        #    artifact is the ONLY path handed to the transcriber — the source
        #    MP4 is never uploaded.
        self._progress(STEP_EXTRACT, "start")
        audio_path = self._extract(metadata, paths.audio)
        self._progress(STEP_EXTRACT, "completed", artifact=str(audio_path))

        # 3. Transcribe (paid). Skip on compatible resume.
        transcribed = False
        if resume and self._validate_resume_step(manifest, STEP_TRANSCRIBE, base_dir=output_dir):
            self._progress(STEP_TRANSCRIBE, "skipped")
            transcript = self._load_transcript_from_artifact(paths.transcript_json)
        else:
            self._progress(STEP_TRANSCRIBE, "start")
            transcriber = self._transcriber_factory(settings)
            result: TranscriptionResult = transcriber.transcribe(
                audio_path=audio_path, metadata=metadata, settings=settings
            )
            transcript = result.transcript
            # Write raw provider payload first (audit), then canonical JSON,
            # then Markdown.
            write_raw_provider_payload(
                paths.raw_dir, step_name=STEP_TRANSCRIBE, payload=result.raw_response
            )
            write_transcript_artifacts(paths, transcript)
            manifest = self._advance(
                manifest,
                step=STEP_TRANSCRIBE,
                artifact_path=paths.transcript_json,
                provider_job_id=result.provider_job_id,
                required_artifacts=(
                    paths.transcript_md,
                    paths.raw_dir / f"{STEP_TRANSCRIBE}.json",
                ),
            )
            write_manifest(manifest, paths.manifest)
            self._progress(
                STEP_TRANSCRIBE,
                "completed",
                artifact=str(paths.transcript_json),
                provider_job_id=result.provider_job_id,
            )
            transcribed = True

        return TranscribeResult(
            transcript=transcript,
            transcribed=transcribed,
            artifact_paths=paths,
        )

    # --- report ----------------------------------------------------------

    def report(
        self,
        *,
        transcript_path: Path,
        settings: Settings,
        output_dir: Path | None = None,
        resume: bool = False,
        force: bool = False,
    ) -> ReportResult:
        """Generate a grounded report from a canonical transcript artifact.

        ``transcript_path`` may be the canonical ``.transcript.json`` or the
        generated ``.transcript.md``; the latter resolves to the adjacent
        ``.transcript.json``. Validates the Anthropic credential before
        constructing the reporter. Writes canonical report JSON before report
        Markdown and atomically advances the manifest (when ``output_dir`` is
        provided so the manifest path is resolvable).
        """

        # Resolve the canonical transcript JSON path.
        canonical = _resolve_transcript_json(transcript_path)
        if not canonical.is_file():
            raise ConfigurationError(
                f"transcript JSON not found for {transcript_path}; expected adjacent "
                f"{canonical.name} to exist and validate.",
                details={"transcript_path": str(transcript_path), "expected": str(canonical)},
            )
        transcript = self._load_transcript_from_artifact(canonical)

        # Determine the source path and output dir for artifact/manifest paths.
        source_path = _source_path_from_transcript(transcript, canonical)
        effective_output_dir = output_dir if output_dir is not None else canonical.parent
        paths = artifact_paths(output_dir=effective_output_dir, source_path=source_path)

        # Validate credentials (report command needs Anthropic only).
        settings.validate_for_command(Command.REPORT)

        # Standalone report uses a SEPARATE manifest path so it cannot
        # overwrite the full-run manifest. Resume identity is keyed on the
        # transcript artifact checksum (content_hash) + config hash, so a
        # modified transcript + ``--resume`` raises ResumeMismatchError rather
        # than returning a stale report.
        transcript_checksum = _safe_checksum(canonical)
        config_hash = self._config_hasher(settings)
        report_manifest_path = paths.report_manifest

        prior: Manifest | None = None
        if resume and not force and report_manifest_path.is_file():
            from video_content_capture.storage.manifest import read_manifest

            candidate = read_manifest(report_manifest_path)
            if candidate is None:
                raise FilesystemError(
                    "Stored report manifest exists but is unreadable; refusing to "
                    "repeat paid work during --resume. Repair it or use --force for "
                    "a new attempt.",
                    details={"path": str(report_manifest_path)},
                )
            if candidate is not None:
                # Content identity: the transcript artifact checksum must
                # match. A modified transcript is an incompatible resume.
                if candidate.content_hash != transcript_checksum:
                    raise ResumeMismatchError(
                        "Stored report manifest is incompatible with the current "
                        "transcript (content changed); start a new attempt with "
                        "--force.",
                        details={
                            "content_hash": {
                                "stored": candidate.content_hash,
                                "current": transcript_checksum,
                            }
                        },
                    )
                if candidate.config_hash != config_hash:
                    raise ResumeMismatchError(
                        "Stored report manifest is incompatible with the current "
                        "report configuration; start a new attempt with --force.",
                        details={
                            "config_hash": {
                                "stored": candidate.config_hash,
                                "current": config_hash,
                            }
                        },
                    )
                prior = candidate

        attempt_id = uuid.uuid4().hex
        manifest = (
            prior
            if prior is not None
            else self._fresh_manifest(
                source_path=source_path,
                content_hash=transcript_checksum,
                config_hash=config_hash,
                attempt_id=attempt_id,
            )
        )

        reported = False
        if resume and self._validate_resume_step(
            manifest, STEP_REPORT, base_dir=effective_output_dir
        ):
            self._progress(STEP_REPORT, "skipped")
            report = self._load_report_from_artifact(paths.report_json)
        else:
            self._progress(STEP_REPORT, "start")
            reporter: Reporter = self._reporter_factory(settings)
            result: ReporterResult = reporter.report(transcript=transcript, settings=settings)
            report = result.report
            # Write raw provider metadata first (audit), then canonical report
            # JSON, then Markdown.
            write_raw_provider_payload(
                paths.raw_dir,
                step_name=STEP_REPORT,
                payload=_metadata_to_bytes(result.raw_metadata),
            )
            write_report_artifacts(
                paths,
                report,
                transcript_markdown_path=canonical.with_suffix(".md"),
            )
            manifest = self._advance(
                manifest,
                step=STEP_REPORT,
                artifact_path=paths.report_json,
                required_artifacts=(
                    paths.report_md,
                    paths.raw_dir / f"{STEP_REPORT}.json",
                ),
            )
            write_manifest(manifest, report_manifest_path)
            self._progress(STEP_REPORT, "completed", artifact=str(paths.report_json))
            reported = True

        return ReportResult(report=report, reported=reported, artifact_paths=paths)

    # --- run -------------------------------------------------------------

    def run(
        self,
        *,
        source_path: Path,
        settings: Settings,
        output_dir: Path,
        resume: bool = False,
        force: bool = False,
    ) -> RunResult:
        """Run the complete pipeline: probe, extract, transcribe, report, cleanup.

        Validates BOTH credentials before any paid call. Performs
        success-only audio cleanup unless ``settings.keep_audio`` is True.
        """

        paths, content_hash, config_hash, attempt_id, prior = self._resolve_settings(
            settings,
            command=Command.RUN,
            output_dir=output_dir,
            source_path=source_path,
            resume=resume,
            force=force,
        )
        manifest = (
            prior
            if (resume and prior is not None)
            else self._fresh_manifest(
                source_path=source_path,
                content_hash=content_hash,
                config_hash=config_hash,
                attempt_id=attempt_id,
            )
        )

        # 1. Probe.
        self._progress(STEP_PROBE, "start")
        metadata = self._probe(source_path)
        self._progress(STEP_PROBE, "completed", container=metadata.container)

        # 2. Extract.
        self._progress(STEP_EXTRACT, "start")
        audio_path = self._extract(metadata, paths.audio)
        self._progress(STEP_EXTRACT, "completed", artifact=str(audio_path))

        # 3. Transcribe (paid). Skip on compatible resume.
        transcribed = False
        if resume and self._validate_resume_step(manifest, STEP_TRANSCRIBE, base_dir=output_dir):
            self._progress(STEP_TRANSCRIBE, "skipped")
            transcript = self._load_transcript_from_artifact(paths.transcript_json)
        else:
            self._progress(STEP_TRANSCRIBE, "start")
            transcriber = self._transcriber_factory(settings)
            tresult: TranscriptionResult = transcriber.transcribe(
                audio_path=audio_path, metadata=metadata, settings=settings
            )
            transcript = tresult.transcript
            write_raw_provider_payload(
                paths.raw_dir, step_name=STEP_TRANSCRIBE, payload=tresult.raw_response
            )
            write_transcript_artifacts(paths, transcript)
            manifest = self._advance(
                manifest,
                step=STEP_TRANSCRIBE,
                artifact_path=paths.transcript_json,
                provider_job_id=tresult.provider_job_id,
                required_artifacts=(
                    paths.transcript_md,
                    paths.raw_dir / f"{STEP_TRANSCRIBE}.json",
                ),
            )
            write_manifest(manifest, paths.manifest)
            self._progress(
                STEP_TRANSCRIBE,
                "completed",
                artifact=str(paths.transcript_json),
                provider_job_id=tresult.provider_job_id,
            )
            transcribed = True

        # 4. Report (paid). Skip on compatible resume.
        reported = False
        if resume and self._validate_resume_step(manifest, STEP_REPORT, base_dir=output_dir):
            self._progress(STEP_REPORT, "skipped")
            report = self._load_report_from_artifact(paths.report_json)
        else:
            self._progress(STEP_REPORT, "start")
            reporter: Reporter = self._reporter_factory(settings)
            rresult: ReporterResult = reporter.report(transcript=transcript, settings=settings)
            report = rresult.report
            write_raw_provider_payload(
                paths.raw_dir,
                step_name=STEP_REPORT,
                payload=_metadata_to_bytes(rresult.raw_metadata),
            )
            write_report_artifacts(
                paths,
                report,
                transcript_markdown_path=paths.transcript_md,
            )
            manifest = self._advance(
                manifest,
                step=STEP_REPORT,
                artifact_path=paths.report_json,
                required_artifacts=(
                    paths.report_md,
                    paths.raw_dir / f"{STEP_REPORT}.json",
                ),
            )
            write_manifest(manifest, paths.manifest)
            self._progress(STEP_REPORT, "completed", artifact=str(paths.report_json))
            reported = True

        # 5. Run metadata (after both artifact sets are durable).
        write_metadata(paths.metadata, manifest.run_metadata)

        # 6. Success-only audio cleanup.
        cleanup_audio(audio_path=audio_path, keep_audio=settings.keep_audio)
        self._progress(
            STEP_CLEANUP,
            "completed",
            kept_audio=settings.keep_audio,
        )

        return RunResult(
            transcript=transcript,
            report=report,
            transcribed=transcribed,
            reported=reported,
            artifact_paths=paths,
        )

    # --- artifact loaders ------------------------------------------------

    def _load_transcript_from_artifact(self, path: Path) -> Transcript:
        if not path.is_file():
            raise ConfigurationError(
                f"transcript artifact not found: {path}",
                details={"path": str(path)},
            )
        try:
            return Transcript.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ConfigurationError(
                f"transcript artifact is not valid canonical JSON: {path}",
                details={"path": str(path), "error": scrub_for_load(str(exc))},
            ) from exc

    def _load_report_from_artifact(self, path: Path) -> Report:
        if not path.is_file():
            raise ConfigurationError(
                f"report artifact not found: {path}",
                details={"path": str(path)},
            )
        try:
            return Report.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise ConfigurationError(
                f"report artifact is not valid canonical JSON: {path}",
                details={"path": str(path), "error": scrub_for_load(str(exc))},
            ) from exc


# --- Helpers --------------------------------------------------------------


def _now_iso() -> str:
    from datetime import datetime

    return datetime.now(UTC).isoformat()


def _resolve_transcript_json(path: Path) -> Path:
    """Resolve a transcript path to its canonical ``.transcript.json`` path.

    A ``.transcript.md`` path resolves to the adjacent ``.transcript.json``.
    Any other path is returned as-is (the caller validates existence).
    """

    name = path.name
    if name.endswith(".transcript.md"):
        return path.with_name(name[: -len(".transcript.md")] + ".transcript.json")
    return path


def _source_path_from_transcript(transcript: Transcript, canonical: Path) -> Path:
    """Recover a source path for artifact naming.

    The canonical transcript carries ``metadata.source_path`` (the original
    source MP4 path). We use its stem to name report artifacts in the same
    directory as the transcript JSON. When the source path is unavailable we
    fall back to the transcript JSON's stem.
    """

    src = transcript.metadata.source_path
    if src:
        return Path(src)
    return canonical.with_suffix(".mp4")


def _safe_checksum(path: Path) -> str:
    try:
        return compute_checksum(path)
    except OSError:
        return ""


def _required_artifact_keys(primary_key: str, step: str) -> tuple[str, ...]:
    """Derive the required Markdown/raw checksum keys for a paid step."""

    primary = Path(primary_key)
    if step == STEP_TRANSCRIBE and primary.name.endswith(".transcript.json"):
        stem = primary.name[: -len(".transcript.json")]
        return (
            str(primary.with_name(f"{stem}.transcript.md")),
            str(primary.parent / f"{stem}.raw" / "transcribe.json"),
        )
    if step == STEP_REPORT and primary.name.endswith(".report.json"):
        stem = primary.name[: -len(".report.json")]
        return (
            str(primary.with_name(f"{stem}.report.md")),
            str(primary.parent / f"{stem}.raw" / "report.json"),
        )
    raise FilesystemError(
        f"completed step {step!r} has an unrecognized primary artifact path",
        details={"step": step, "path": primary_key},
    )


def _metadata_to_bytes(metadata: dict[str, Any]) -> bytes:
    import json

    return json.dumps(metadata, ensure_ascii=False, default=str).encode("utf-8")


def scrub_for_load(text: str) -> str:
    """Redact secrets when embedding error text into a ConfigurationError.

    Imported lazily-safe: this is a thin wrapper so the pipeline does not
    hard-depend on the redaction module at import time (avoiding cycles).
    """

    from video_content_capture.redaction import scrub_text

    return scrub_text(text)


__all__ = [
    "Pipeline",
    "ProbeResult",
    "ReportResult",
    "RunResult",
    "TranscribeResult",
    "STEP_CLEANUP",
    "STEP_EXTRACT",
    "STEP_PROBE",
    "STEP_REPORT",
    "STEP_TRANSCRIBE",
]
