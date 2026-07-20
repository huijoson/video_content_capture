## Context

The workspace has one source asset and no application code: a 1.545 GB, 34:20 MP4 containing HEVC video, one AAC stereo audio track, and no subtitles. Local `ffmpeg`, `ffprobe`, Python 3.13, and `uv` are available. The content is primarily Mandarin financial commentary, the number of speakers is unknown, cloud processing is permitted, and the required outputs are a timestamped Traditional Chinese transcript plus a plain-language report for general readers.

The design must avoid unnecessary video processing, preserve evidence for financial claims, tolerate long-running cloud jobs, and make paid operations resumable. The first release is a local CLI, but the core pipeline must not depend on terminal presentation.

## Goals / Non-Goals

**Goals:**

- Provide `probe`, `transcribe`, `report`, and `run` CLI commands.
- Upload only an extracted audio artifact, never the full MP4.
- Produce provider-neutral transcript data with stable segment IDs, timestamps, raw text, normalized Traditional Chinese text, and deterministic speaker labels.
- Generate a structured, evidence-grounded report whose source references are validated before Markdown rendering.
- Support retries, atomic manifests, idempotent cache keys, interruption recovery, and explicit cleanup behavior.
- Keep credentials out of logs and generated artifacts.
- Build the implementation through TDD with unit, integration, opt-in live API, and full-video acceptance checks.

**Non-Goals:**

- Web UI, HTTP API, queue, database, user accounts, multi-tenant storage, or background workers.
- Video frame analysis, OCR, face recognition, speaker identity recognition, or editing the source video.
- Fully local speech recognition or diarization in the first release.
- Fact-checking the source program against external financial data or producing investment advice.
- Automatically archiving the OpenSpec change or publishing code.

## Decisions

### 1. Use a modular Python package with a thin Typer CLI

`pipeline.py` coordinates provider-neutral domain services. Typer converts command arguments to validated settings and translates typed domain errors into stable exit codes. Terminal progress is injected through a callback interface rather than printed from core modules.

**Alternatives considered:** A single script is faster to start but couples subprocesses, provider calls, rendering, and state recovery. A Web service adds file lifecycle and deployment concerns that are not required for the first release.

### 2. Extract the audio stream without decoding video

The media layer probes with `ffprobe`, then uses an argument-array `ffmpeg` subprocess to map only the selected audio stream. For the target file it first attempts AAC stream-copy into `.m4a`, preserving quality and avoiding HEVC decoding. A controlled audio-only transcode is used only if the configured transcription provider rejects the source codec.

**Alternatives considered:** Uploading the original MP4 wastes bandwidth and exposes video content. Decoding to WAV is larger and unnecessary for a managed API that accepts compressed audio.

### 3. Use provider adapters and a managed diarizing transcriber

A `Transcriber` protocol returns a provider-neutral `Transcript`. The first adapter uses AssemblyAI for Mandarin recognition, speaker labels, and word/segment timestamps. Provider-specific payloads remain confined to `transcription/assemblyai.py`, and the raw response is retained for recovery and audit.

The adapter submits one full audio artifact when provider limits permit, preserving consistent speaker identities across the 34-minute program. Chunking is a limit-driven fallback, not the default; overlap de-duplication and speaker reconciliation are required when chunking occurs.

**Alternatives considered:** Local Whisper plus pyannote creates Python 3.13, Apple MPS, model-download, performance, and diarization-alignment risks. A provider-specific pipeline without an adapter would make replacement costly.

### 4. Treat structured JSON as canonical and Markdown as a rendering

Canonical transcript JSON stores source metadata, raw provider text, normalized text, words, segment timing, stable segment IDs, and speaker labels. Canonical report JSON stores section types, plain-language content, and source segment IDs. Markdown renderers contain no provider or LLM logic and deterministically render validated models.

When `vcc report` receives a generated transcript Markdown path, it resolves the adjacent transcript JSON instead of reparsing prose.

**Alternatives considered:** Using Markdown as the only source loses word timing and makes validation and repeatable report generation fragile.

### 5. Normalize conservatively

Normalization improves punctuation and converts safe character variants to Traditional Chinese while preserving the raw recognized text. Numbers, currencies, organization names, stock symbols, and uncertain financial terms are not silently rewritten. Low-confidence content remains visible rather than being guessed by a language model.

**Alternatives considered:** Aggressive LLM cleanup improves fluency but can change amounts, entities, and claims.

### 6. Generate reports from evidence IDs, not model timestamps

Before implementing the Claude adapter, the implementation session invokes the `claude-api` skill to use the current Anthropic SDK, structured-output mechanism, and supported model identifier. The reporting model receives structured transcript segments with stable IDs.

For long transcripts, map steps create chronological topic candidates that retain source IDs. A reduce step merges and deduplicates candidates without dropping their evidence. Grounding validation rejects unknown IDs, empty evidence, invalid section shapes, and out-of-range references. Markdown timestamps and links are generated by code from the validated transcript segments; the model never supplies authoritative timestamps.

**Alternatives considered:** Free-form summarization is simpler but can invent timestamps and unsupported financial details. A second external fact-checking system is beyond scope.

### 7. Use content-and-configuration keyed resumable artifacts

The storage layer computes a streamed SHA-256 of the source and combines it with the effective processing configuration. Each step writes deterministic artifacts and atomically replaces `manifest.json` only after successful completion. `--resume` reuses valid completed steps, while `--force` starts a new attempt. Configuration mismatches fail explicitly rather than reusing stale paid results.

Default cleanup removes extracted audio only after final outputs succeed, retains raw transcription JSON, and honors `--keep-audio`.

**Alternatives considered:** Timestamp-only cache directories cannot prove that source content or provider settings match. In-memory state cannot recover from interruption.

### 8. Bound retries and classify errors

Only timeouts, HTTP 429, and 5xx failures receive bounded exponential retries. Authentication, invalid requests, unsupported media, malformed provider responses, and grounding failures stop immediately with typed errors. Ctrl-C preserves the last atomically completed manifest step.

**Alternatives considered:** Retrying every failure can repeat invalid paid requests and hide configuration errors.

### 9. Use subagent-driven TDD and a main-agent completion gate

The implementation session uses `superpowers:subagent-driven-development`, with a fresh implementation subagent for each task and requirement/code-quality review checkpoints. Each task follows `superpowers:test-driven-development`. After all tasks, `superpowers:verification-before-completion` runs fresh verification, and the main `gpt-5.6-sol` agent performs the final review rather than delegating that conclusion to an implementation subagent.

No commit, push, publication, or OpenSpec archival occurs without explicit user instruction.

## Risks / Trade-offs

- **Cloud transcription cost or outage** → Cache provider job identifiers and raw responses, retry only transient failures, and make live tests opt-in.
- **Speaker diarization assigns inconsistent labels** → Prefer one full-audio job, expose optional speaker-count bounds, use anonymous deterministic labels, and manually sample the final output.
- **ASR changes financial numbers or entities** → Preserve raw text, normalize conservatively, surface uncertainty, and require manual acceptance sampling.
- **LLM report introduces unsupported claims** → Require structured evidence IDs, validate every section, derive timestamps in code, and label source forecasts as speaker opinions.
- **Provider API evolves** → Isolate SDK use in adapters, pin dependencies in `uv.lock`, keep model/provider settings configurable, and verify live documentation before writing each adapter.
- **Large local artifacts consume disk** → Use compressed audio, deterministic cache locations, success-only cleanup, and documented retention controls.
- **Python 3.13 dependency incompatibility** → Avoid local ML dependencies in the first release and verify the locked cloud/CLI dependency set under Python 3.13.
- **A future Web UI needs asynchronous progress** → Keep the pipeline and progress callback independent from Typer without adding Web infrastructure now.

## Migration Plan

This is a greenfield project with no existing application or stored schema to migrate.

1. Create and lock the Python package and tests.
2. Verify credential-free media probing against the target MP4.
3. Verify transcription and reporting with mocked provider fixtures.
4. Run an opt-in 30–60 second live excerpt and confirm speaker/timestamp grounding.
5. Run the complete 34:20 file only after short-sample acceptance.
6. If a live provider integration fails, retain the provider-neutral models, fixtures, and CLI contracts and replace only the adapter; source media remains untouched.

## Open Questions

No architecture decisions block implementation. Live transcription and Anthropic credentials are execution prerequisites, and exact provider/model identifiers remain configuration values verified against current provider documentation during their implementation tasks.
