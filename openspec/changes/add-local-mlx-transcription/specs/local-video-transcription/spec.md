## ADDED Requirements

### Requirement: Explicit local transcription selection
The system SHALL expose an `mlx` transcription backend through CLI and environment configuration and
SHALL retain the existing AssemblyAI backend as a separate selectable option.

#### Scenario: Select local backend from CLI
- **WHEN** the user runs `vcc transcribe <video> --transcription-backend mlx`
- **THEN** the system selects the MLX adapter and does not construct an AssemblyAI client

#### Scenario: Preserve existing default
- **WHEN** no transcription backend is specified
- **THEN** the system retains the documented AssemblyAI default behavior

### Requirement: Credential-free on-device inference
The system SHALL transcribe extracted audio locally with MLX Whisper without requiring a cloud API
key and SHALL NOT upload the source media or extracted audio.

#### Scenario: Local transcription without credentials
- **WHEN** `mlx` is selected and no provider credentials are configured
- **THEN** transcription proceeds on-device using the configured MLX Whisper model

#### Scenario: Missing local runtime
- **WHEN** `mlx` is selected on an unsupported platform or without the required runtime
- **THEN** the command fails before inference with an actionable configuration error and does not
  fall back to a cloud backend

### Requirement: Canonical local transcript mapping
The MLX adapter SHALL map recognized segments and words into the existing canonical transcript model
with stable IDs, ordered in-range timestamps, preserved raw text, conservatively normalized
Traditional Chinese text, and deterministic Markdown anchors.

#### Scenario: Map a valid MLX payload
- **WHEN** MLX Whisper returns valid segment and word timestamps for Mandarin audio
- **THEN** canonical JSON and Markdown contain ordered `sNNNN` segments, corresponding word timing,
  raw and normalized text, and anchors derived from those segment IDs

#### Scenario: Reject malformed local output
- **WHEN** MLX Whisper returns missing/non-string text, no usable nonempty segments, invalid timing,
  or timing outside the media duration
- **THEN** the adapter fails with a typed provider-payload error before marking transcription complete

#### Scenario: Ignore empty silence segments
- **WHEN** MLX Whisper returns an empty or whitespace-only segment alongside usable speech segments
- **THEN** the adapter omits the empty segment and preserves the remaining ordered transcript

### Requirement: Honest speaker fallback
The local backend SHALL label every transcript segment `講者 A` and SHALL NOT claim diarization or
infer additional speaker identities.

#### Scenario: Local transcript has anonymous speaker labels
- **WHEN** local transcription returns one or more segments
- **THEN** every canonical segment is labeled `講者 A`

#### Scenario: Reject unsupported speaker bounds
- **WHEN** `mlx` is selected with `--min-speakers` or `--max-speakers`
- **THEN** configuration fails before inference and explains that local diarization is unavailable

### Requirement: Backend-sensitive resume identity
The system SHALL include transcription backend and local model identity in its processing
configuration hash so incompatible transcripts are never silently reused.

#### Scenario: Resume with identical local configuration
- **WHEN** a completed local transcript is resumed with the same source, backend, and model
- **THEN** the system reuses the validated artifacts without running MLX inference again

#### Scenario: Reject changed backend or model
- **WHEN** a user resumes after changing the transcription backend or MLX model
- **THEN** the system reports a resume mismatch and requires an explicit fresh attempt

### Requirement: Progressive local acceptance
The project SHALL validate a short audio-only excerpt locally before processing the full source and
SHALL record manual transcript quality evidence without claiming speaker diarization.

#### Scenario: Accept local excerpt before full source
- **WHEN** the local backend is ready for the target 34:20 video
- **THEN** a 30–60 second excerpt is transcribed and checked for readable text, ordered in-range
  timestamps, anonymous fallback labels, canonical anchors, and compatible resume before the full run
