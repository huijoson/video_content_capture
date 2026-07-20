"""Validated configuration for the Video Content Capture CLI.

Configuration is loaded from environment variables and may be overridden by
explicit constructor arguments (explicit > environment > default). Cloud
credentials are read from the environment only, are never logged, and are
redacted from ``repr``/``str`` via :class:`pydantic.SecretStr`.

Probe is credential-free: ``validate_for_command(Command.PROBE)`` succeeds
without any cloud key. ``transcribe`` requires the AssemblyAI key, ``report``
requires the Anthropic key, and ``run`` requires both before any paid or
provider work begins.

The default Anthropic reporting model is the verified
``claude-opus-4-8`` (a member of the installed ``anthropic`` SDK's
``ModelParam`` literal as of SDK 0.117.0, verified 2026-07-20). It is
env-overridable via ``VCC_DEFAULT_ANTHROPIC_MODEL``.
"""

from __future__ import annotations

import enum
import os
from pathlib import Path
from typing import Literal, cast

from pydantic import BaseModel, ConfigDict, Field, SecretStr, model_validator


class ConfigError(Exception):
    """Raised when configuration is missing or invalid for a command."""


class Command(enum.Enum):
    """The CLI command whose configuration requirements are being checked."""

    PROBE = "probe"
    TRANSCRIBE = "transcribe"
    REPORT = "report"
    RUN = "run"


# Commands that require cloud provider credentials.
_TRANSCRIBE_COMMANDS = frozenset({Command.TRANSCRIBE, Command.RUN})
_REPORT_COMMANDS = frozenset({Command.REPORT, Command.RUN})

# Bounded, provider-neutral retry defaults.
_DEFAULT_MAX_RETRIES = 5
_DEFAULT_RETRY_BASE_DELAY_SECONDS = 1.0
_DEFAULT_LANGUAGE = "zh-TW"
_DEFAULT_TRANSCRIPTION_BACKEND: Literal["assemblyai", "mlx"] = "assemblyai"
_DEFAULT_MLX_WHISPER_MODEL = "mlx-community/whisper-large-v3-turbo"
# Verified against the installed anthropic SDK (0.117.0) on 2026-07-20:
# ``claude-opus-4-8`` is a member of the SDK's ``ModelParam`` literal, so the
# SDK accepts it directly as the ``model`` argument of ``messages.parse`` /
# ``messages.create``. This is the current Anthropic Opus model used by the
# reporting adapter; it is env-overridable via ``VCC_DEFAULT_ANTHROPIC_MODEL``.
_DEFAULT_ANTHROPIC_MODEL = "claude-opus-4-8"
_DEFAULT_ASSEMBLYAI_MODEL = "best"


def _env(name: str) -> str | None:
    value = os.environ.get(name)
    if value is None or value == "":
        return None
    return value


def _env_secret(name: str) -> SecretStr | None:
    value = _env(name)
    if value is None:
        return None
    return SecretStr(value)


def _env_int(name: str, default: int) -> int:
    value = _env(name)
    if value is None:
        return default
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


def _env_int_opt(name: str) -> int | None:
    """Parse an optional integer env var.

    Returns ``None`` only when the variable is unset or empty. An explicit
    ``"0"`` is preserved as ``0`` so :meth:`Settings.validate_speaker_bounds`
    can surface it as a clear validation error rather than silently treating
    it as automatic estimation.
    """

    value = _env(name)
    if value is None:
        return None
    try:
        return int(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be an integer, got {value!r}") from exc


def _env_float(name: str, default: float) -> float:
    value = _env(name)
    if value is None:
        return default
    try:
        return float(value)
    except ValueError as exc:
        raise ConfigError(f"{name} must be a number, got {value!r}") from exc


def _env_path(name: str) -> Path | None:
    value = _env(name)
    if value is None:
        return None
    return Path(value)


def _env_transcription_backend() -> Literal["assemblyai", "mlx"]:
    value = _env("VCC_TRANSCRIPTION_BACKEND") or _DEFAULT_TRANSCRIPTION_BACKEND
    # Pydantic validates the runtime value against the Literal field; the cast
    # keeps the default factory precise for strict static typing.
    return cast("Literal['assemblyai', 'mlx']", value)


class Settings(BaseModel):
    """Effective runtime configuration for one CLI invocation.

    Precedence: explicit constructor argument > environment variable > default.
    """

    model_config = ConfigDict(
        arbitrary_types_allowed=True,
        validate_assignment=False,
        extra="forbid",
    )

    # Output and cache locations.
    output_dir: Path | None = Field(default_factory=lambda: _env_path("VCC_OUTPUT_DIR"))
    cache_dir: Path | None = Field(default_factory=lambda: _env_path("VCC_CACHE_DIR"))

    # Language and transcription provider/model settings.
    language: str = Field(default_factory=lambda: _env("VCC_LANGUAGE") or _DEFAULT_LANGUAGE)
    transcription_backend: Literal["assemblyai", "mlx"] = Field(
        default_factory=_env_transcription_backend,
        validate_default=True,
    )
    assemblyai_model: str = Field(
        default_factory=lambda: _env("VCC_ASSEMBLYAI_MODEL") or _DEFAULT_ASSEMBLYAI_MODEL
    )
    mlx_whisper_model: str = Field(
        default_factory=lambda: _env("VCC_MLX_WHISPER_MODEL") or _DEFAULT_MLX_WHISPER_MODEL
    )
    anthropic_model: str = Field(
        default_factory=lambda: _env("VCC_DEFAULT_ANTHROPIC_MODEL") or _DEFAULT_ANTHROPIC_MODEL
    )

    # Speaker diarization bounds. ``None`` means automatic estimation.
    # An explicit ``0`` is preserved so validate_speaker_bounds can surface it
    # as a clear error rather than silently collapsing to automatic mode.
    min_speakers: int | None = Field(default_factory=lambda: _env_int_opt("VCC_MIN_SPEAKERS"))
    max_speakers: int | None = Field(default_factory=lambda: _env_int_opt("VCC_MAX_SPEAKERS"))

    # Retry behaviour.
    max_retries: int = Field(
        default_factory=lambda: _env_int("VCC_MAX_RETRIES", _DEFAULT_MAX_RETRIES)
    )
    retry_base_delay_seconds: float = Field(
        default_factory=lambda: _env_float(
            "VCC_RETRY_BASE_DELAY_SECONDS", _DEFAULT_RETRY_BASE_DELAY_SECONDS
        )
    )

    # Cleanup behaviour.
    keep_audio: bool = False

    # Credentials — environment only, redacted from all repr/str output.
    assemblyai_api_key: SecretStr | None = Field(
        default_factory=lambda: _env_secret("ASSEMBLYAI_API_KEY"),
        repr=False,
    )
    anthropic_api_key: SecretStr | None = Field(
        default_factory=lambda: _env_secret("ANTHROPIC_API_KEY"),
        repr=False,
    )

    @model_validator(mode="after")
    def _validate_ranges(self) -> Settings:
        if not self.mlx_whisper_model.strip():
            raise ConfigError("mlx_whisper_model must be nonempty")
        if self.max_retries < 1:
            raise ConfigError("max_retries must be at least 1")
        if self.retry_base_delay_seconds <= 0:
            raise ConfigError("retry_base_delay_seconds must be positive")
        return self

    # --- Speaker bounds ---------------------------------------------------

    def validate_speaker_bounds(self) -> None:
        """Validate optional speaker-count bounds for diarization."""

        if self.min_speakers is None and self.max_speakers is None:
            return
        if self.min_speakers is not None and self.min_speakers < 1:
            raise ConfigError(f"min_speakers must be >= 1, got {self.min_speakers}")
        if self.max_speakers is not None and self.max_speakers < 1:
            raise ConfigError(f"max_speakers must be >= 1, got {self.max_speakers}")
        if (
            self.min_speakers is not None
            and self.max_speakers is not None
            and self.min_speakers > self.max_speakers
        ):
            raise ConfigError(
                f"min_speakers ({self.min_speakers}) must not exceed "
                f"max_speakers ({self.max_speakers})"
            )

    # --- Per-command credential validation --------------------------------

    def validate_for_command(self, command: Command) -> None:
        """Validate that configuration is sufficient for the given command.

        ``probe`` is credential-free. ``transcribe`` requires the AssemblyAI
        key, ``report`` requires the Anthropic key, and ``run`` requires both.
        Cloud commands also validate speaker bounds.
        """

        if command is Command.PROBE:
            return

        if (
            command in _TRANSCRIBE_COMMANDS
            and self.transcription_backend == "mlx"
            and (self.min_speakers is not None or self.max_speakers is not None)
        ):
            raise ConfigError(
                "MLX transcription does not provide speaker diarization; "
                "omit min_speakers and max_speakers"
            )

        # Speaker bounds are relevant for every non-probe command. The local
        # unsupported-option error above takes precedence for transcribe/run.
        self.validate_speaker_bounds()

        if command in _TRANSCRIBE_COMMANDS:
            if self.transcription_backend == "assemblyai" and not self._has_assemblyai():
                raise ConfigError(
                    f"ASSEMBLYAI_API_KEY is required for the {command.value!r} command"
                )
        if command in _REPORT_COMMANDS and not self._has_anthropic():
            raise ConfigError(f"ANTHROPIC_API_KEY is required for the {command.value!r} command")

    def _has_assemblyai(self) -> bool:
        secret = self.assemblyai_api_key
        return secret is not None and secret.get_secret_value() != ""

    def _has_anthropic(self) -> bool:
        secret = self.anthropic_api_key
        return secret is not None and secret.get_secret_value() != ""


__all__ = ["Command", "ConfigError", "Settings"]
