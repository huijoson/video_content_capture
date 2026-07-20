## Why

The workspace currently contains a 34-minute Mandarin financial-program MP4 with no subtitle track and no reusable processing code. A repeatable CLI is needed to extract the spoken content, preserve timestamps and speaker turns, and turn the transcript into a grounded Traditional Chinese report that general readers can understand.

## What Changes

- Add a Python 3.13 CLI for probing media, transcribing a video, generating a report from a transcript, or running the complete pipeline.
- Extract and upload only the audio stream instead of decoding or uploading the 1.5 GB HEVC video.
- Add managed Mandarin speech recognition with automatic speaker diarization and timestamps behind a provider-neutral adapter.
- Produce a canonical transcript JSON file and a human-readable Traditional Chinese Markdown transcript with timestamped speaker sections.
- Add Claude-based, evidence-grounded report generation that explains financial content in plain language and links every source-dependent point to validated transcript segments.
- Add deterministic artifact paths, manifests, retries, idempotency, and resumable execution to avoid repeating paid API operations.
- Add credential-safe configuration, typed errors, progress output, tests, and operating documentation.
- Keep the first release CLI-only while separating the core pipeline from terminal presentation so a future interface can reuse it.

## Capabilities

### New Capabilities

- `video-content-capture-cli`: Probe MP4 media, extract speech, transcribe Mandarin with timestamps and speaker labels, render transcript artifacts, generate a grounded plain-language report, and resume interrupted processing through a CLI.

### Modified Capabilities

None.

## Impact

- Adds a new `src/video_content_capture/` Python package, command `vcc`, test suite, project configuration, and documentation.
- Adds runtime dependencies for CLI/configuration, managed transcription, Anthropic report generation, retry handling, and terminal progress.
- Requires local `ffmpeg`/`ffprobe` executables and cloud API credentials for transcription and reporting commands; media probing remains credential-free.
- Creates local output and cache artifacts containing extracted audio, provider responses, structured transcript/report data, Markdown documents, and execution metadata.
- Does not modify the source MP4 and does not introduce a database, queue, Web server, or background service.
