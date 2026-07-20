# 2026-07-20 Replace Cloud Acceptance with Local MLX Transcription

## Goal and acceptance criteria

- [x] Transcribe the target MP4 locally without AssemblyAI or Anthropic credentials.
- [x] Use the installed MLX Whisper runtime and cached large-v3-turbo model; do not upload media.
- [x] Preserve canonical transcript JSON/Markdown, ordered in-range timestamps, raw text, and
  conservative Traditional Chinese normalization.
- [x] Represent the lack of local diarization honestly with an anonymous single-speaker fallback.
- [x] Prove the local path on a short excerpt before processing the complete 34:20 source.

## Plan

- [x] Stop the cloud live-acceptance plan after the user chose a local-only conversion.
- [x] Inspect local hardware, installed runtimes, cached models, and provider boundaries.
- [x] Create an OpenSpec change for a local MLX Whisper transcriber.
- [x] Add failing focused tests, then implement the smallest local backend and CLI/config wiring.
- [x] Verify a short local excerpt and compatible resume behavior.
- [x] Process the full source locally and inspect transcript artifacts.

## Working notes

- Local runtime: Apple Silicon with 48 GB RAM; `mlx_whisper` 0.4.3 is installed.
- Cached model: `mlx-community/whisper-large-v3-turbo` (about 1.5 GB), so no model download is
  required for the selected path.
- Ollama is installed, but only a cloud-backed model is present. A local report model is outside
  this minimal conversion slice; the first result will be canonical transcript JSON/Markdown.
- MLX Whisper provides segment/word timestamps but no speaker diarization. Local mode must use
  `講者 A` consistently and document that limitation instead of inferring identities.
- The Traditional Chinese initial-prompt experiment did not improve script consistency and was
  rejected. Output preserves raw ASR text and uses the existing conservative normalizer.
- Human auditory comparison is not possible in this interface. Five distributed transcript samples
  are recorded below, but OpenSpec task 4.3 remains unchecked until a person compares playback.

## Dependencies and environment

- Existing Python 3.13, `ffmpeg`/`ffprobe`, `mlx_whisper`, MLX, and cached model.
- No API keys and no cloud-provider calls.

## Risk and rollback

- Risk: medium; this changes production backend selection and the credential contract.
- Rollback: retain the existing AssemblyAI adapter and make local selection explicit/reversible;
  revert the new adapter/config wiring if local quality or compatibility is unacceptable.

## Results

- Added OpenSpec change `add-local-mlx-transcription`; 12/13 tasks are complete after recording this
  evidence. Only the five-sample human listening portion of 4.3 remains open.
- Implemented explicit `mlx` backend selection, local credential behavior, conditional dependency,
  backend/model resume identity, lazy adapter loading, canonical mapping, and `講者 A` fallback.
- Excerpt acceptance: 45.002-second AAC, 38 ordered/in-range segments, complete anchors, retained raw
  payload, and compatible resume with unchanged transcript/raw artifacts.
- Full output: 2,060.167 seconds, 1,807 ordered/in-range segments, complete anchors, no overlap over
  50 ms, no gap over 10 seconds, and a retained 1,385,731-byte raw local payload.
- Full compatible `--resume` skipped MLX and left raw/JSON/Markdown sizes and mtimes unchanged.
- Acceptance exposed and fixed: missing creation of a new ffmpeg output directory, relative manifest
  path double-prefixing on resume, and empty MLX silence segments aborting long transcription.
- Five distributed samples for human playback comparison:
  - 00:00:59 (`s0034`): `好還有就是完全替代人類通用劳動力的機器人`
  - 00:07:00 (`s0339`): `有什么区别吗`
  - 00:13:59 (`s0730`): `本来這個环境就很恶劣`
  - 00:20:59 (`s1097`): `稍前周期很長`
  - 00:29:59 (`s1577`): `如果有要想做這支股票的朋友`
- Final verification passed: `uv sync --dev`, Ruff, format check, strict mypy, and 360 offline tests.
- Canonical outputs are under `outputs/local-full/`; no report was generated and no cloud call ran.

---

# 2026-07-20 Resume Video CLI Live Acceptance

## Goal and acceptance criteria

- [ ] Complete OpenSpec tasks 12.1–12.5 only when both cloud credentials are intentionally available.
- [ ] Accept a 30–60 second excerpt before submitting the complete 34:20 source.
- [ ] Prove transcript timing/speakers/anchors, report grounding, and paid-call-safe resume behavior.
- [ ] Manually sample five full-source timestamps without rewriting unsupported ASR content.

## Plan

- [x] Locate the active OpenSpec change and read every apply context artifact.
- [x] Check live-test prerequisites without exposing credential values.
- [x] Validate the handoff and reconfirm the credential-free live gate/source probe.
- [ ] Create and accept the short audio-only excerpt.
- [ ] Verify the compatible `--resume` run does not rewrite raw provider responses.
- [ ] Run and inspect the full source only after excerpt acceptance.
- [ ] Mark OpenSpec tasks 12.1–12.5 complete only from observed live evidence.

## Working notes

- Active change: `build-video-transcript-report-cli` (`spec-driven`, 58/63 complete).
- Authoritative pending list: `openspec/changes/build-video-transcript-report-cli/tasks.md`, group 12.
- `ASSEMBLYAI_API_KEY`, `ANTHROPIC_API_KEY`, `VCC_ENABLE_LIVE`, and
  `VCC_LIVE_EXCERPT_PATH` remain absent in the resumed environment.
- No live or paid command was run. OpenSpec task 12.6 requires 12.1–12.5 to remain unchecked.
- Resume at the credential-presence check, then follow the README live-test command exactly.
- `docs/checkpoints/current-handoff.md` passed resume validation at 11,469 bytes; targeted
  OpenSpec state remains `58/63` with only tasks 12.1–12.5 pending.
- A passing `tests/test_live_acceptance.py` is necessary but insufficient for group 12: its
  unchanged-mtime assertion must be supplemented with provider call records for 12.3, plus the
  manual transcript/report, full-source, and five-playback checks required by 12.2–12.5.

## Dependencies and environment

- Python `>=3.13,<3.14`, `uv`, `ffmpeg`, and `ffprobe`.
- Intentional access to valid AssemblyAI and Anthropic API credentials.
- Target source: `視野環球財經robots_07-19-2026 22-11-19_1.MP4`.

## Risk and rollback

- Risk: medium because acceptance invokes paid cloud APIs and processes source-program audio.
- Guardrail: both `pytest -m live` and `VCC_ENABLE_LIVE=1` are required; excerpt acceptance gates the full upload.
- Rollback: stop on any excerpt failure; retain manifests/artifacts for diagnosis and use compatible `--resume`
  rather than repeating completed paid work.

## Results

- Live acceptance is blocked by missing intentional credentials and opt-in variables.
- Safe gate verification passed: with all live variables explicitly unset,
  `uv run pytest -m live tests/test_live_acceptance.py -q` exited 0 with one skipped test.
- Credential-free source probe passed: duration `2060.167s`, one HEVC video stream, one AAC stereo
  audio stream, and no subtitle streams.
- No application code or OpenSpec checkbox was changed during this continuation.

---

# 2026-07-20 Codex Context Handoff Skill

## Goal and acceptance criteria

- [x] Install a global `context-handoff` Skill under `~/.codex/skills`.
- [x] Support bounded `checkpoint` and drift-aware `resume` modes.
- [x] Archive and validate `docs/checkpoints/current-handoff.md` deterministically.
- [x] Emit a shell-safe command that starts a fresh Codex CLI session.
- [x] Verify structure, scripts, edge cases, and realistic forward use.

## Plan

- [x] Confirm environment and initialize the Skill with the official scaffold.
- [x] Implement the deterministic checkpoint helper and self-test.
- [x] Write concise Skill instructions and matching UI metadata.
- [x] Run compile, self-test, Skill validation, and isolated smoke checks.
- [x] Forward-test checkpoint and resume behavior with fresh subagents.
- [x] Review the final artifacts and record results.

## Working notes

- Target surface: Codex CLI.
- Trigger policy: user invokes the Skill when the existing `context-used` TUI item reaches 80%.
- Automation: reliable semi-automatic; no hooks, transcript JSONL parsing, compaction, or automatic session launch.
- Checkpoint path: `docs/checkpoints/current-handoff.md`; resume continues automatically unless material drift is found.
- Workspace is not a Git repository, so verification must not assume Git is available.
- First self-test exposed macOS `/var` versus `/private/var` path identity; the test now compares resolved paths. Checkpoint validation itself passed.
- Initial CLI smoke batch was rejected before execution because its cleanup used `rm -rf`; rerun uses only `rmdir` on known-empty temporary directories.
- Verification then found a Python 3.13 Ruff import rule and zsh's read-only `status` parameter; both are corrected before rerunning the full check set.
- Post-check inventory found generated `__pycache__`; self-test now disables import bytecode and compiles only into a temporary directory.
- Independent review found symlink escape, permission widening, unbounded archive/read, and invalid-bootstrap risks; implementation now fails closed, publishes `0600` archives atomically, and validates before bootstrap.
- A shell smoke falsely passed after its valid fixture had been removed; CLI success and failure paths now have independent return-code assertions inside the self-test.
- Re-review found a parent-directory TOCTOU window; all checkpoint and archive operations now use no-follow, descriptor-relative directory traversal and I/O, with a parent-replacement regression case.

## Dependencies and environment

- Codex personal Skill root: `/Users/yuhan/.codex/skills`.
- Python standard library only for runtime scripts.
- Existing `/Users/yuhan/.codex/config.toml` already displays `context-used`; no config change is required.

## Risk and rollback

- Risk: low; this adds a personal Skill and does not change hooks or Codex configuration.
- Rollback: remove `/Users/yuhan/.codex/skills/context-handoff`.
- Generated project checkpoints remain intact for audit unless explicitly removed.

## Results

- Installed `/Users/yuhan/.codex/skills/context-handoff` with `SKILL.md`, UI metadata, a deterministic checkpoint helper, and an artifact-free self-test.
- Helper enforces the 16 KiB contract, descriptor-relative no-follow path traversal, bounded reads, private atomic archives, structural validation, and validation-gated bootstrap output.
- Verification passed: Skill validator, bundled self-test, Ruff, strict mypy, checkpoint/resume forward-tests, and independent security review.
- Fresh checkpoint/resume forward-test completed the recorded task with 2 passing tests; latest security-hardened checkpoint forward-test archived two generations as `0600`, validated the new checkpoint, and left product source unchanged.
- Final review reported no remaining Critical or Important findings.
# 2026-07-20 Publish Initial GitHub Repository

## Goal and acceptance criteria

- [ ] Create `huijoson/video_content_capture` from the current workspace as a private repository.
- [ ] Include source, tests, project docs, OpenSpec artifacts, and dependency lock data.
- [ ] Exclude source media, generated outputs, credentials, caches, and local agent/session state.
- [ ] Push a verified initial `main` commit and confirm the remote branch matches locally.

## Plan

- [x] Verify GitHub CLI authentication and confirm the target repository name is available.
- [x] Audit the workspace for large files, credentials, generated artifacts, and local-only state.
- [x] Add publish safeguards and review the exact initial commit scope.
- [x] Run the repository verification suite and create the initial commit.
- [ ] Create the private GitHub repository, push `main`, and verify remote state.

## Working notes

- GitHub CLI is authenticated as `huijoson`; `huijoson/video_content_capture` does not yet exist.
- The root source MP4 is approximately 1.5 GB and must remain local.
- Repository visibility defaults to private because the user did not request public exposure.
- Initial repositories have no pre-existing default branch to target with a pull request, so the
  reviewed initial commit will establish `main` directly.

## Dependencies and environment

- GitHub CLI `gh` with authenticated `repo` scope.
- Local `git`, Python `>=3.13,<3.14`, `uv`, and the existing project verification toolchain.

## Risk and rollback

- Risk: medium because publication copies local content to an external service.
- Guardrails: private visibility, explicit ignore rules, credential scan, staged-file and size review.
- Rollback: delete the new GitHub repository if publication scope is wrong; the local workspace and
  ignored media remain unchanged.

## Results

- Publish scope review passed: 73 intended files totaling approximately 1 MiB; the 1.5 GB source
  video, generated outputs, caches, local settings, and checkpoints are ignored.
- Credential-pattern review found only blank `.env.example` placeholders and a deliberately fake
  regression-test secret.
- Verification passed: `uv sync --dev`, Ruff lint, Ruff format check, strict mypy, and 360 offline
  tests. GitHub creation and push remain pending.

---
