## 1. Execution Setup and TDD Discipline

- [x] 1.1 In the implementation session, invoke `/opsx:apply` for `build-video-transcript-report-cli`, then invoke `superpowers:subagent-driven-development` before modifying application code.
- [x] 1.2 Create a tracked implementation task per numbered group, dispatch a fresh subagent for each group, and perform requirement and code-quality review checkpoints before accepting the group.
- [x] 1.3 Invoke or follow `superpowers:test-driven-development` for every behavior change: add a failing focused test, confirm the expected failure, implement the minimum behavior, and rerun the focused test before broader checks.
- [x] 1.4 Do not commit, push, publish, or archive the OpenSpec change unless the user explicitly requests that external or history-changing action.

## 2. Python Package and Configuration

- [x] 2.1 Add failing tests for configuration precedence, `zh-TW` defaults, speaker-bound validation, credential-lazy `probe`, and credential-required cloud commands.
- [x] 2.2 Create `pyproject.toml`, `.python-version`, `.env.example`, `.gitignore`, package entry files, and a `vcc` Typer console script for Python `>=3.13,<3.14` managed by `uv`.
- [x] 2.3 Implement `src/video_content_capture/config.py` with validated paths, language, speaker bounds, provider/model settings, retry limits, cleanup settings, and environment-based secrets.
- [x] 2.4 Add development dependencies and verify the initial package with `uv sync --dev`, `uv run ruff check .`, `uv run mypy src`, and the focused configuration tests.

## 3. Provider-Neutral Domain Contracts

- [x] 3.1 Add failing model tests for timing validation, deterministic segment IDs, anonymous speaker labels, source-segment evidence, and invalid report evidence shapes.
- [x] 3.2 Implement `domain/models.py` models for media metadata, words, transcript segments, transcripts, report sections, reports, run metadata, and processing-step state.
- [x] 3.3 Implement `domain/errors.py` typed errors and stable categories for media, configuration, provider authentication, rate limits, provider payloads, grounding, resume mismatch, and filesystem failures.
- [x] 3.4 Run focused domain tests, then Ruff and mypy for the new modules.

## 4. Media Probe and Audio Extraction

- [x] 4.1 Add fixtures and failing tests for ffprobe JSON parsing, missing files, no-audio media, Chinese paths with spaces, AAC stream-copy arguments, and post-extraction validation.
- [x] 4.2 Implement `media/probe.py` using argument-array `ffprobe` execution and provider-neutral `VideoMetadata` parsing.
- [x] 4.3 Implement `media/audio.py` to map only the selected audio stream, prefer AAC-to-M4A stream-copy for the target file, and use a controlled audio-only transcode fallback without decoding video frames.
- [x] 4.4 Verify `uv run vcc probe "視野環球財經robots_07-19-2026 22-11-19_1.MP4"` reports the approximately 34:20 duration, one audio stream, and no subtitle streams without cloud credentials.
- [x] 4.5 Run focused media tests and inspect captured subprocess arguments to confirm the original MP4 is never configured as the transcription upload artifact.

## 5. Artifact Storage, Manifest, and Resume

- [x] 5.1 Add failing tests for streamed SHA-256 cache keys, configuration-sensitive keys, deterministic output names, atomic manifest replacement, compatible resume, incompatible resume rejection, force behavior, and success-only audio cleanup.
- [x] 5.2 Implement `storage/paths.py` for readable source-based filenames and content/configuration-keyed cache locations.
- [x] 5.3 Implement `storage/manifest.py` with typed step state, atomic temporary-file replacement, provider job identifiers, artifact checksums, and interruption-safe reads.
- [x] 5.4 Implement `storage/artifacts.py` for raw provider responses, transcript/report JSON, Markdown outputs, metadata, cleanup, and `--keep-audio` behavior.
- [x] 5.5 Run focused storage tests, including a simulated interruption that leaves the previous valid manifest readable.

## 6. Managed Mandarin Transcription and Diarization

- [x] 6.1 Verify current AssemblyAI documentation and Python 3.13 package compatibility for Mandarin transcription, speaker labels, timestamps, speaker-count controls, uploads, and long-running job polling; pin the verified dependency in `uv.lock`.
- [x] 6.2 Add provider-response fixtures and failing tests for successful multi-speaker mapping, automatic speaker estimation, optional speaker bounds, raw-text preservation, malformed responses, authentication failure, 429/5xx retry, timeout retry, and non-retryable 4xx errors.
- [x] 6.3 Define the `Transcriber` protocol in `transcription/base.py` and implement the AssemblyAI adapter in `transcription/assemblyai.py` without leaking provider types into domain or pipeline modules.
- [x] 6.4 Prefer one full-audio job; add tested limit-driven chunk fallback with bounded overlap, duplicate removal, timestamp offsets, and stable speaker reconciliation.
- [x] 6.5 Implement `transcription/normalize.py` for conservative punctuation and Traditional Chinese normalization while preserving raw text and avoiding silent changes to numbers, currencies, entities, stock symbols, and uncertain terms.
- [x] 6.6 Run focused transcription tests with mocked network calls and confirm the default suite makes no paid API requests.

## 7. Canonical Transcript and Markdown Rendering

- [x] 7.1 Add failing serialization and snapshot tests for canonical transcript JSON, source metadata, stable anchors, Traditional Chinese headings, timestamp formatting, quoted segment text, and anonymous speakers.
- [x] 7.2 Implement `markdown/transcript.py` as a deterministic renderer with no provider calls or normalization logic.
- [x] 7.3 Write `<stem>.transcript.json` before `<stem>.transcript.md` and include a visible warning that automated speech recognition can contain errors.
- [x] 7.4 Verify every Markdown segment anchor maps to exactly one canonical segment ID and every displayed time lies within media duration.

## 8. Grounded Claude Report Generation

- [x] 8.1 Invoke the `claude-api` skill before opening or writing the Anthropic adapter, then select the current supported SDK, structured-output mechanism, and model identifier and expose the model through configuration.
- [x] 8.2 Add failing tests with mocked Claude responses for required report sections, chronological map groups, evidence-preserving reduction, unknown segment IDs, empty evidence, malformed output, speaker-opinion labeling, and secret-free errors.
- [x] 8.3 Define the `Reporter` protocol in `reporting/base.py` and evidence-bearing structured response models in the reporting package.
- [x] 8.4 Implement `reporting/prompts.py` so the model receives structured transcript segments, writes for general readers in Traditional Chinese, explains necessary financial terms, avoids investment advice, and returns source segment IDs instead of timestamps.
- [x] 8.5 Implement `reporting/claude.py` with bounded chronological map/reduce processing, retry classification, and no free-form Markdown generation inside the API adapter.
- [x] 8.6 Implement `reporting/grounding.py` to reject unknown IDs, evidence-free source claims, invalid section shapes, and out-of-range evidence before rendering.
- [x] 8.7 Run focused reporting tests and confirm all rendered timestamps will be derived from canonical transcript segments rather than model text.

## 9. Plain-Language Report Rendering

- [x] 9.1 Add failing JSON and Markdown snapshot tests for `三分鐘掌握影片`, `核心重點`, `重要數字與說法`, `名詞白話解釋`, `結論與可能影響`, and `來源索引`.
- [x] 9.2 Implement `markdown/report.py` as a deterministic renderer from validated report JSON and canonical transcript evidence.
- [x] 9.3 Generate transcript-anchor links and timestamp labels in code from source segment IDs, and label forecasts or recommendations as `講者觀點`.
- [x] 9.4 Verify `<stem>.report.json` is successfully validated and written before `<stem>.report.md`.

## 10. Pipeline and CLI Integration

- [x] 10.1 Add failing integration tests for `probe`, `transcribe`, `report`, and `run`, including Chinese paths, Markdown-to-adjacent-JSON resolution, resume after transcription, force behavior, stable error exits, and Ctrl-C state preservation.
- [x] 10.2 Implement `pipeline.py` use cases that coordinate probe, extraction, transcription, artifact writing, reporting, cleanup, and resumable manifests without terminal-specific output.
- [x] 10.3 Implement `cli.py` and `__main__.py` with Rich progress through an injectable callback and options for output directory, language, speaker bounds, `--resume`, `--force`, `--keep-audio`, and verbosity.
- [x] 10.4 Map typed error categories to documented nonzero exit codes and ensure logs redact all configured credentials.
- [x] 10.5 Run CLI integration tests and a second compatible mocked `--resume` run that asserts no duplicate transcription or reporting request.

## 11. Documentation and Offline Verification

- [x] 11.1 Document prerequisites, `uv` setup, `ffmpeg` checks, credentials, commands, output schemas, privacy implications, estimated cloud-cost inputs, cleanup, retries, resume behavior, exit codes, and troubleshooting in `README.md`.
- [x] 11.2 Add repository-owned synthetic media fixtures and mocked provider fixtures; mark all live API tests with an explicit opt-in pytest marker.
- [x] 11.3 Run `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, and `uv run pytest -q` and fix every failure without weakening tests.
- [x] 11.4 Search captured logs and generated fixture artifacts for API-key values and verify no secret is present.

## 12. Progressive Live Acceptance

- [ ] 12.1 If live credentials are available, create a temporary 30–60 second audio-only excerpt from the target video and run the complete live pipeline on that excerpt before submitting the full source.
- [ ] 12.2 Verify the excerpt transcript has ordered in-range timestamps, at least one anonymous speaker label, readable Traditional Chinese, canonical anchors, and a report whose every source-dependent item has valid evidence links.
- [ ] 12.3 Rerun the excerpt with `--resume` and verify provider call records show no duplicate paid transcription for completed compatible state.
- [ ] 12.4 Only after excerpt acceptance, run the complete 34:20 source and verify the system uploads audio rather than the 1.545 GB MP4, produces transcript/report JSON and Markdown, and completes without unexplained large gaps or overlap duplicates.
- [ ] 12.5 Manually compare five sampled transcript timestamps against playback for text and speaker plausibility; record any ASR uncertainty without rewriting unsupported content.
- [x] 12.6 If credentials are unavailable, leave live tasks unchecked and report them as skipped prerequisites rather than claiming live verification.

## 13. Main-Agent Review and Completion Gate

- [x] 13.1 Invoke `superpowers:verification-before-completion` and rerun all format, lint, type, unit, integration, and completed live acceptance checks from fresh output.
- [x] 13.2 Have the main `gpt-5.6-sol` agent review the complete working tree for requirement coverage, correctness, security, credential handling, provider retries, grounding guarantees, resume/idempotency, unnecessary complexity, and test quality.
- [x] 13.3 Process confirmed review findings with `superpowers:receiving-code-review`, add regression tests before fixes, and rerun focused plus complete verification.
- [x] 13.4 Optionally invoke `simplify` only after correctness is established, and rerun the complete verification suite after any simplification edits.
- [x] 13.5 Update these OpenSpec checkboxes to reflect actual results and do not archive the change until the user explicitly requests `/opsx:archive`.
