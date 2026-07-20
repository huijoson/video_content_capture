## ADDED Requirements

### Requirement: Credential-free media probing
The system SHALL provide a `vcc probe <video>` command that validates the input file and reports container, duration, video streams, audio streams, and subtitle streams without requiring cloud credentials or modifying the source file.

#### Scenario: Probe the target MP4
- **WHEN** the user runs `vcc probe` with the existing Mandarin financial-program MP4
- **THEN** the command reports the approximately 34-minute duration, one audio stream, and absence of subtitle streams without calling a cloud API

#### Scenario: Reject media without audio
- **WHEN** the user probes a readable media file that contains no audio stream
- **THEN** the command exits with a typed media error and a nonzero media-specific exit code

### Requirement: Audio-only extraction
The system SHALL extract only the selected audio stream for transcription and SHALL NOT decode video frames or upload the source MP4 when the audio stream is sufficient.

#### Scenario: Extract AAC from HEVC MP4
- **WHEN** the user transcribes the target HEVC MP4
- **THEN** the media layer maps the AAC audio stream to a cached audio artifact without decoding HEVC video frames

#### Scenario: Handle Chinese filenames safely
- **WHEN** the input path contains Chinese characters and spaces
- **THEN** the media subprocess receives an argument array and processes the exact path without shell interpolation

### Requirement: Managed Mandarin transcription with diarization
The system SHALL transcribe Mandarin audio through the configured managed provider and SHALL return provider-neutral segments containing stable IDs, start and end times, recognized text, and anonymous speaker labels.

#### Scenario: Automatic speaker estimation
- **WHEN** the user does not provide speaker-count bounds
- **THEN** the transcription request enables automatic speaker estimation and maps provider speakers to deterministic labels such as `講者 A` and `講者 B`

#### Scenario: User supplies speaker bounds
- **WHEN** the user provides valid `--min-speakers` and `--max-speakers` values
- **THEN** the system passes the supported bounds to the configured provider and validates that the minimum does not exceed the maximum

#### Scenario: Preserve unknown identity
- **WHEN** diarization distinguishes multiple voices but the source does not establish their names
- **THEN** the system SHALL retain anonymous speaker labels and SHALL NOT infer personal identities

### Requirement: Full-audio speaker consistency
The system SHALL prefer one full-audio transcription job when provider size and duration limits permit, and SHALL apply overlap de-duplication and speaker reconciliation when chunking is required.

#### Scenario: Provider accepts the complete audio
- **WHEN** the extracted 34-minute audio is within configured provider limits
- **THEN** the system submits one transcription job so speaker labels remain consistent across the complete program

#### Scenario: Provider requires chunks
- **WHEN** the extracted audio exceeds a provider limit
- **THEN** the system creates bounded overlapping chunks, removes duplicated overlap text, and maps chunk-local speakers to stable transcript speaker labels

### Requirement: Conservative Traditional Chinese normalization
The system SHALL retain raw recognized text and SHALL produce a normalized Traditional Chinese representation without silently changing numbers, currencies, organization names, stock symbols, or financial claims.

#### Scenario: Preserve a financial amount
- **WHEN** a transcript segment contains a recognized numeric amount and currency
- **THEN** normalization preserves the amount and currency unless a deterministic formatting rule can be applied without changing meaning

#### Scenario: Retain uncertain recognition
- **WHEN** recognized wording has low confidence or cannot be safely normalized
- **THEN** the system preserves the recognized wording or marks uncertainty instead of guessing a replacement

### Requirement: Canonical transcript artifacts
The system SHALL write a canonical transcript JSON artifact and a deterministic Traditional Chinese Markdown transcript for each successful transcription.

#### Scenario: Render timestamped speaker sections
- **WHEN** normalized transcript segments are available
- **THEN** the Markdown contains source metadata and one anchored section per segment in the form `HH:MM:SS–HH:MM:SS｜講者 X`

#### Scenario: Preserve reportable evidence
- **WHEN** transcript artifacts are written
- **THEN** the JSON retains stable segment IDs and timing data, and the Markdown anchors correspond to those IDs

### Requirement: Evidence-grounded plain-language reporting
The system SHALL generate a Traditional Chinese report for general readers from structured transcript segments, and every source-dependent report item SHALL include one or more valid source segment IDs.

#### Scenario: Generate the required report sections
- **WHEN** the user runs `vcc report` on a valid transcript artifact
- **THEN** the report contains a short overview, core topics, important numbers or claims, plain-language glossary, conclusion or possible impact, and a source index

#### Scenario: Reject unknown evidence
- **WHEN** a model response references a segment ID that is absent from the canonical transcript
- **THEN** grounding validation rejects the response before a report Markdown file is rendered

#### Scenario: Derive timestamps from evidence
- **WHEN** a validated report item references known transcript segments
- **THEN** the renderer derives timestamps and transcript links from those segments rather than trusting model-generated timestamps

#### Scenario: Distinguish speaker opinion
- **WHEN** the source speaker makes a forecast, judgment, or recommendation
- **THEN** the report identifies it as a speaker viewpoint and does not present it as independently verified fact or investment advice

### Requirement: Long-transcript report reduction
The system SHALL process transcripts that exceed a single safe reporting context by using chronological map results and an evidence-preserving reduce step.

#### Scenario: Reduce multiple transcript groups
- **WHEN** the transcript is divided into multiple reporting groups
- **THEN** each mapped topic retains source segment IDs and the reduced report merges or deduplicates topics without dropping all evidence from any retained point

### Requirement: Resumable and idempotent processing
The system SHALL key processing state by source content and effective configuration, SHALL persist completed steps atomically, and SHALL avoid repeating completed paid API operations during a compatible resume.

#### Scenario: Resume after transcription
- **WHEN** transcription completed but report generation was interrupted
- **THEN** `--resume` loads the valid transcript artifacts and starts from report generation without submitting another transcription request

#### Scenario: Reject incompatible resume state
- **WHEN** the source content or a processing setting differs from the manifest
- **THEN** the system refuses to reuse the incompatible state and explains how to start a new attempt

#### Scenario: Force a new attempt
- **WHEN** the user supplies `--force`
- **THEN** the system starts a new processing attempt without treating prior completed steps as current

### Requirement: Controlled artifact retention
The system SHALL retain structured provider and transcript data needed for audit and recovery, and SHALL apply explicit success-only audio cleanup behavior.

#### Scenario: Default successful cleanup
- **WHEN** a complete run succeeds without `--keep-audio`
- **THEN** the system removes the extracted audio artifact while retaining raw transcription JSON, canonical transcript/report JSON, Markdown outputs, manifest, and run metadata

#### Scenario: Keep extracted audio
- **WHEN** a complete run succeeds with `--keep-audio`
- **THEN** the extracted audio remains in the deterministic cache location

#### Scenario: Preserve failure evidence
- **WHEN** processing fails before final output
- **THEN** the system retains the last valid manifest and artifacts required to diagnose or resume the attempt

### Requirement: Credential-safe configuration
The system SHALL read cloud credentials from environment-based configuration, SHALL validate credentials only for commands that require the corresponding provider, and SHALL redact credentials from logs and generated artifacts.

#### Scenario: Probe without credentials
- **WHEN** no cloud API keys are configured and the user runs `vcc probe`
- **THEN** probing succeeds if the local media and tools are valid

#### Scenario: Cloud command without credential
- **WHEN** the required provider key is absent and the user runs `transcribe`, `report`, or `run`
- **THEN** the command fails before upload with a clear configuration error and SHALL NOT print another configured secret

### Requirement: Classified retries and failures
The system SHALL retry only transient timeouts, rate limits, and server failures with bounded exponential backoff, and SHALL expose stable nonzero exit codes for media, configuration, provider, grounding, and filesystem failures.

#### Scenario: Transient provider failure
- **WHEN** the provider returns a timeout, HTTP 429, or HTTP 5xx response before the retry limit
- **THEN** the system retries with bounded exponential delay and records the attempt without duplicating completed steps

#### Scenario: Permanent provider failure
- **WHEN** the provider returns authentication failure, invalid input, unsupported media, or a malformed successful payload
- **THEN** the system stops without transient retry and exits with the corresponding provider or configuration error code

### Requirement: CLI command surface
The system SHALL expose `probe`, `transcribe`, `report`, and `run` commands through the `vcc` console entry point.

#### Scenario: Run the complete pipeline
- **WHEN** the user runs `vcc run <video>` with valid tools, configuration, and credentials
- **THEN** the system probes media, extracts audio, transcribes and diarizes speech, writes transcript artifacts, generates a grounded report, writes metadata, and reports the output paths

#### Scenario: Generate report from Markdown path
- **WHEN** the user runs `vcc report` with a transcript Markdown file generated by the system
- **THEN** the command resolves and validates the adjacent canonical transcript JSON before report generation

### Requirement: Interruptible progress reporting
The system SHALL report step progress through an injectable progress interface and SHALL preserve the last atomically completed state when interrupted.

#### Scenario: User interrupts a cloud wait
- **WHEN** the user presses Ctrl-C while the CLI waits for transcription or reporting
- **THEN** the command exits without corrupting the manifest and a later compatible `--resume` can continue from the last completed step

### Requirement: Verification without routine cloud spending
The project SHALL include unit and integration tests that use owned fixtures and mocked provider responses, and SHALL mark live API tests as explicit opt-in tests.

#### Scenario: Run the default test suite
- **WHEN** a developer runs the documented default pytest command without live-test credentials
- **THEN** all unit and integration tests run without making paid cloud API requests

#### Scenario: Validate the target video progressively
- **WHEN** live credentials are intentionally supplied for acceptance testing
- **THEN** the documented process validates a 30–60 second excerpt before submitting the complete 34-minute source
