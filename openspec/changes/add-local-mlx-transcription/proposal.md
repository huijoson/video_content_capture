## Why

The video cannot complete cloud acceptance because provider credentials are unavailable, while the
target Apple Silicon machine already has MLX Whisper and a cached high-quality Mandarin-capable
model. The CLI needs an explicit local transcription path that never uploads media or requires an
API key.

## What Changes

- Add an `mlx` transcription backend for Apple Silicon that converts extracted audio into the
  existing canonical transcript JSON and Markdown artifacts entirely on-device.
- Add backend/model configuration and CLI selection while retaining the AssemblyAI adapter as an
  explicit compatible option.
- Make credential validation depend on the selected backend so local transcription does not require
  `ASSEMBLYAI_API_KEY`.
- Preserve ordered segment and word timestamps, raw recognized text, conservative Traditional
  Chinese normalization, resumable artifacts, and source-media privacy.
- Represent MLX Whisper's lack of diarization honestly by applying the anonymous single-speaker
  label `講者 A`; speaker-count bounds remain unsupported for this backend.
- Keep Anthropic report generation unchanged and outside this local transcription slice.

## Capabilities

### New Capabilities

- `local-video-transcription`: Select and run an on-device MLX Whisper transcription backend that
  produces canonical transcript artifacts without credentials or network uploads.

### Modified Capabilities

None.

## Impact

- Affects transcription configuration, CLI options/default factory wiring, config hashing, and the
  provider-neutral `Transcriber` implementation set.
- Adds an Apple-Silicon-only `mlx-whisper` runtime dependency and local model-path configuration.
- Adds focused adapter/config/CLI tests and local excerpt/full-source acceptance evidence.
- Does not remove the AssemblyAI or Anthropic adapters, change canonical artifact schemas, upload
  the source MP4, or archive the existing OpenSpec change.
