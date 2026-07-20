## Context

The current CLI has provider-neutral `Transcriber` and pipeline boundaries, but production wiring
always constructs `AssemblyAITranscriber`, and configuration always requires an AssemblyAI key for
`transcribe`. The target is an M4 Pro Mac with 48 GB RAM, `mlx_whisper` 0.4.3 installed globally,
and a complete cached `mlx-community/whisper-large-v3-turbo` model. The project `uv` environment does
not yet include MLX Whisper.

The local path must preserve the existing canonical transcript models, renderers, manifests, and
resume guarantees. It must not claim speaker diarization that MLX Whisper does not provide, and it
must never upload media. Report generation remains separately backed by Anthropic and is not needed
to convert this video into transcript artifacts.

## Goals / Non-Goals

**Goals:**

- Add an explicit `mlx` transcription backend on Apple Silicon.
- Produce the same canonical transcript JSON and Markdown shape from MLX segment/word timestamps.
- Avoid transcription credentials and all media uploads in local mode.
- Include backend/model identity in the resume configuration hash.
- Preserve AssemblyAI as a selectable backend without changing its behavior.
- Verify a short excerpt before processing the full 34:20 source.

**Non-Goals:**

- Local speaker diarization, speaker identity recognition, or fabricated multi-speaker labels.
- Local report generation or changes to the Anthropic reporting adapter.
- Supporting MLX on non-Apple-Silicon platforms.
- Removing cloud dependencies or rewriting existing provider-neutral domain/storage contracts.

## Decisions

### 1. Add explicit backend selection and keep the existing default

Add `transcription_backend` with values `assemblyai` and `mlx`, sourced from
`VCC_TRANSCRIPTION_BACKEND` and `--transcription-backend`. Keep `assemblyai` as the default for
backward compatibility; the requested conversion will explicitly use `mlx`. Credential validation
requires `ASSEMBLYAI_API_KEY` only when a transcription-bearing command selects `assemblyai`.

**Alternative considered:** silently replace the production default. Rejected because it changes
existing latency, platform, speaker, and dependency behavior for all users.

### 2. Implement MLX behind the existing `Transcriber` protocol

Add `transcription/mlx.py` with a lazy MLX Whisper import. Invoke `mlx_whisper.transcribe` on the
already-extracted audio with the configured model, Mandarin language mapping, deterministic decoding,
and word timestamps. Translate the returned dictionary into existing `Transcript`,
`TranscriptSegment`, and `Word` models, sort by start time, clamp only insignificant floating-point
overflow at media duration, omit empty silence segments, reject missing/non-string text and
malformed/out-of-range payloads, and assign stable `sNNNN` IDs.

Raw local output is retained as UTF-8 JSON through the existing raw-artifact writer. The local job ID
records backend/model identity but is not a remote handle.

**Alternative considered:** shell out to the globally installed `mlx_whisper` command. Rejected
because the project environment would depend on mutable `PATH` state, payload parsing would be less
testable, and interruption/error handling would be weaker.

### 3. Use an honest single-speaker fallback

Every local segment receives `講者 A`. Supplying speaker-count bounds with `mlx` fails before
inference with a clear configuration error because those options imply unavailable diarization.

**Alternative considered:** infer speaker changes from pauses or alternating segments. Rejected
because that would fabricate evidence unsupported by the local model.

### 4. Add an Apple-Silicon conditional dependency

Add `mlx-whisper>=0.4.3,<0.5` guarded by Darwin/ARM64 environment markers. The adapter checks the
platform and dependency at construction and raises a typed configuration error otherwise. The model
setting defaults to `mlx-community/whisper-large-v3-turbo` and accepts a local path or compatible
repository identifier through `VCC_MLX_WHISPER_MODEL`.

**Alternative considered:** install diarization stacks such as pyannote/WhisperX. Rejected for this
slice because they add large model/token/runtime requirements and do not help the immediate
credential-free conversion.

### 5. Preserve resume correctness with backend/model identity

Add `transcription_backend` and `mlx_whisper_model` to `compute_config_hash`. A transcript produced by
AssemblyAI cannot be reused as an MLX result, and changing the local model requires `--force` rather
than silently mixing artifacts.

## Risks / Trade-offs

- **No diarization in local mode** → label all segments `講者 A`, reject speaker bounds, and state the
  limitation in CLI/README/output acceptance notes.
- **Model output may contain Simplified Chinese or ASR errors** → preserve raw text, apply only the
  existing conservative normalizer, and manually sample the final transcript.
- **MLX or model missing on another machine** → fail before inference with the exact installation or
  model setting needed; never fall back to cloud automatically.
- **Long inference or memory growth** → verify a short excerpt first, retain resumable artifacts, and
  use the already-cached large-v3-turbo model rather than a larger checkpoint.
- **Model repository identifiers can trigger downloads** → document that inference remains local but
  model acquisition can use the network; the target acceptance uses the verified existing cache.

## Migration Plan

1. Add config/hash tests that fail before implementation.
2. Add MLX payload mapping tests with an injected transcription function; no model inference in the
   default suite.
3. Add CLI factory/option tests and implement the adapter/wiring.
4. Sync the conditional dependency and run format, lint, type, and offline tests.
5. Create a 45-second audio-only excerpt and run local transcription with the cached model.
6. Inspect timing/text/anchors and compatible `--resume`, then process the full source locally.
7. Roll back by selecting `assemblyai` or reverting the new adapter/config fields; existing cloud
   artifacts and adapters remain intact.

## Open Questions

None block implementation. Local report generation can be proposed separately if transcript-only
artifacts are insufficient after the user reviews the result.
