## 1. Configuration and Dependency Contract

- [x] 1.1 Add failing tests for backend/model precedence, AssemblyAI-key requirements by backend,
  unsupported local speaker bounds, and backend/model-sensitive config hashes.
- [x] 1.2 Add the conditional MLX Whisper dependency plus validated `transcription_backend` and
  `mlx_whisper_model` settings with backward-compatible defaults.
- [x] 1.3 Run focused configuration/storage tests, Ruff, and mypy for the changed modules.

## 2. Local MLX Transcriber

- [x] 2.1 Add failing fixture-driven tests for valid segment/word mapping, stable IDs, ordered
  in-range timestamps, raw/normalized text, `講者 A`, malformed payloads, and platform/runtime errors.
- [x] 2.2 Implement `transcription/mlx.py` behind the existing `Transcriber` protocol using an
  injectable MLX transcription function and secret-free raw JSON retention.
- [x] 2.3 Run focused local-transcription tests and confirm the default suite performs no model
  inference, download, or network request.

## 3. CLI Wiring and Documentation

- [x] 3.1 Add failing CLI tests for `--transcription-backend mlx`, backend-specific factory wiring,
  credential-free local transcription, and unchanged AssemblyAI selection.
- [x] 3.2 Implement CLI/config wiring for the local backend without changing report behavior or
  automatically falling back between local and cloud backends.
- [x] 3.3 Update README and environment examples with local setup, cached-model behavior, privacy,
  no-diarization limits, and exact local transcription/resume commands.

## 4. Verification and Local Acceptance

- [x] 4.1 Run `uv sync --dev`, Ruff check/format, mypy, and the complete offline pytest suite.
- [x] 4.2 Create and probe a temporary 30–60 second audio-only excerpt, transcribe it with the cached
  large-v3-turbo model, and inspect timing, text, labels, anchors, raw output, and resume reuse.
- [ ] 4.3 Only after excerpt acceptance, transcribe the complete 34:20 source locally and inspect the
  canonical JSON/Markdown for gaps, range errors, and five sampled playback timestamps.
- [x] 4.4 Record exact verification evidence in `tasks/todo.md`; do not archive, commit, push, or
  publish either OpenSpec change without explicit user instruction.
