"""Focused configuration tests for video_content_capture.

Covers OpenSpec group 2: configuration precedence, zh-TW defaults,
speaker-bound validation, credential-lazy ``probe``, and credential-required
cloud commands. Credentials must be read from the environment and redacted
from repr/logs.
"""

from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from video_content_capture.config import Command, ConfigError, Settings


@pytest.fixture(autouse=True)
def _isolate_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Strip cloud credentials from the environment for every test."""

    for key in (
        "ASSEMBLYAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "VCC_LANGUAGE",
        "VCC_OUTPUT_DIR",
        "VCC_CACHE_DIR",
        "VCC_TRANSCRIPTION_BACKEND",
        "VCC_MLX_WHISPER_MODEL",
    ):
        monkeypatch.delenv(key, raising=False)


# --- Defaults ------------------------------------------------------------


def test_default_language_is_zh_tw() -> None:
    settings = Settings()
    assert settings.language == "zh-TW"


def test_default_cleanup_keeps_audio_false() -> None:
    settings = Settings()
    assert settings.keep_audio is False


def test_default_retry_limits_are_bounded() -> None:
    settings = Settings()
    assert settings.max_retries >= 1
    assert settings.retry_base_delay_seconds > 0


def test_default_transcription_backend_remains_assemblyai() -> None:
    settings = Settings()
    assert settings.transcription_backend == "assemblyai"


def test_default_mlx_whisper_model_is_large_v3_turbo() -> None:
    settings = Settings()
    assert settings.mlx_whisper_model == "mlx-community/whisper-large-v3-turbo"


def test_blank_mlx_whisper_model_is_rejected() -> None:
    with pytest.raises(ConfigError, match="mlx_whisper_model"):
        Settings(mlx_whisper_model="  ")


# --- Precedence ----------------------------------------------------------


def test_env_overrides_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VCC_LANGUAGE", "en")
    settings = Settings()
    assert settings.language == "en"


def test_explicit_overrides_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("VCC_LANGUAGE", "en")
    settings = Settings(language="ja")
    assert settings.language == "ja"


def test_local_backend_and_model_follow_explicit_env_default_precedence(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCC_TRANSCRIPTION_BACKEND", "mlx")
    monkeypatch.setenv("VCC_MLX_WHISPER_MODEL", "env-model")
    env_settings = Settings()
    assert env_settings.transcription_backend == "mlx"
    assert env_settings.mlx_whisper_model == "env-model"

    explicit = Settings(transcription_backend="assemblyai", mlx_whisper_model="explicit-model")
    assert explicit.transcription_backend == "assemblyai"
    assert explicit.mlx_whisper_model == "explicit-model"


def test_invalid_transcription_backend_from_environment_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("VCC_TRANSCRIPTION_BACKEND", "invalid")
    with pytest.raises(ValidationError, match="transcription_backend"):
        Settings()


def test_credentials_loaded_from_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "assembly-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    settings = Settings()
    assert settings.assemblyai_api_key is not None
    assert settings.assemblyai_api_key.get_secret_value() == "assembly-secret"
    assert settings.anthropic_api_key is not None
    assert settings.anthropic_api_key.get_secret_value() == "anthropic-secret"


# --- Speaker bounds ------------------------------------------------------


def test_speaker_bounds_valid_when_min_le_max() -> None:
    settings = Settings(min_speakers=2, max_speakers=4)
    settings.validate_speaker_bounds()


def test_speaker_bounds_invalid_when_min_exceeds_max() -> None:
    settings = Settings(min_speakers=4, max_speakers=2)
    with pytest.raises(ConfigError, match="speaker"):
        settings.validate_speaker_bounds()


def test_speaker_bounds_invalid_when_min_below_one() -> None:
    settings = Settings(min_speakers=0, max_speakers=2)
    with pytest.raises(ConfigError, match="speaker"):
        settings.validate_speaker_bounds()


def test_speaker_bounds_invalid_when_max_below_min() -> None:
    settings = Settings(min_speakers=5, max_speakers=3)
    with pytest.raises(ConfigError, match="speaker"):
        settings.validate_speaker_bounds()


# --- Env-parsed speaker bounds ------------------------------------------


def test_env_min_speakers_zero_raises_instead_of_silent_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VCC_MIN_SPEAKERS=0 must surface a clear ConfigError, not silently
    collapse to ``None`` (which would mean automatic estimation)."""

    monkeypatch.setenv("VCC_MIN_SPEAKERS", "0")
    settings = Settings()
    # The explicit ``0`` must be retained and surfaced as a validation error,
    # not silently turned into ``None`` / automatic estimation.
    assert settings.min_speakers == 0
    with pytest.raises(ConfigError, match="min_speakers"):
        settings.validate_speaker_bounds()


def test_env_max_speakers_zero_raises_instead_of_silent_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """VCC_MAX_SPEAKERS=0 must surface a clear ConfigError, not silently
    collapse to ``None`` (which would mean automatic estimation)."""

    monkeypatch.setenv("VCC_MAX_SPEAKERS", "0")
    settings = Settings()
    assert settings.max_speakers == 0
    with pytest.raises(ConfigError, match="max_speakers"):
        settings.validate_speaker_bounds()


def test_env_speaker_bounds_unset_means_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """No env var set => automatic estimation, both bounds None."""

    settings = Settings()
    assert settings.min_speakers is None
    assert settings.max_speakers is None


def test_env_speaker_bounds_empty_string_means_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """An empty VCC_MIN_SPEAKERS / VCC_MAX_SPEAKERS is treated as unset."""

    monkeypatch.setenv("VCC_MIN_SPEAKERS", "")
    monkeypatch.setenv("VCC_MAX_SPEAKERS", "")
    settings = Settings()
    assert settings.min_speakers is None
    assert settings.max_speakers is None


# --- Credential-lazy probe ----------------------------------------------


def test_probe_does_not_require_cloud_credentials() -> None:
    settings = Settings()
    # Must not raise: probing is credential-free.
    settings.validate_for_command(Command.PROBE)


# --- Credential-required cloud commands ---------------------------------


def test_transcribe_requires_assemblyai_credential() -> None:
    settings = Settings()
    with pytest.raises(ConfigError, match="(?i)assemblyai"):
        settings.validate_for_command(Command.TRANSCRIBE)


def test_local_transcribe_does_not_require_cloud_credentials() -> None:
    Settings(transcription_backend="mlx").validate_for_command(Command.TRANSCRIBE)


@pytest.mark.parametrize("min_speakers", [0, 1])
def test_local_transcribe_rejects_speaker_bounds(min_speakers: int) -> None:
    settings = Settings(transcription_backend="mlx", min_speakers=min_speakers)
    with pytest.raises(ConfigError, match="(?i)diarization"):
        settings.validate_for_command(Command.TRANSCRIBE)


def test_local_run_requires_anthropic_but_not_assemblyai() -> None:
    settings = Settings(transcription_backend="mlx")
    with pytest.raises(ConfigError, match="(?i)anthropic"):
        settings.validate_for_command(Command.RUN)

    Settings(
        transcription_backend="mlx",
        anthropic_api_key="anthropic-secret",
    ).validate_for_command(Command.RUN)


def test_report_requires_anthropic_credential() -> None:
    settings = Settings(assemblyai_api_key="assembly-secret")
    with pytest.raises(ConfigError, match="(?i)anthropic"):
        settings.validate_for_command(Command.REPORT)


def test_run_requires_both_credentials() -> None:
    settings = Settings(assemblyai_api_key="assembly-secret")
    with pytest.raises(ConfigError, match="(?i)anthropic"):
        settings.validate_for_command(Command.RUN)

    settings_only_anthropic = Settings(anthropic_api_key="anthropic-secret")
    with pytest.raises(ConfigError, match="(?i)assemblyai"):
        settings_only_anthropic.validate_for_command(Command.RUN)


def test_cloud_command_succeeds_when_credentials_present() -> None:
    settings = Settings(
        assemblyai_api_key="assembly-secret",
        anthropic_api_key="anthropic-secret",
    )
    settings.validate_for_command(Command.RUN)


# --- Credential redaction ------------------------------------------------


def test_credentials_redacted_from_repr(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "assembly-secret")
    monkeypatch.setenv("ANTHROPIC_API_KEY", "anthropic-secret")
    settings = Settings()
    text = repr(settings)
    assert "assembly-secret" not in text
    assert "anthropic-secret" not in text
    # Credential field names must not appear in the repr at all.
    assert "assemblyai_api_key" not in text
    assert "anthropic_api_key" not in text


def test_credentials_redacted_from_str(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ASSEMBLYAI_API_KEY", "assembly-secret")
    settings = Settings()
    assert "assembly-secret" not in str(settings)


# --- Paths ----------------------------------------------------------------


def test_default_output_dir_is_cwd_outputs(tmp_path: Path) -> None:
    settings = Settings()
    assert settings.output_dir is None or isinstance(settings.output_dir, Path)


def test_explicit_output_dir_resolved(tmp_path: Path) -> None:
    settings = Settings(output_dir=tmp_path)
    assert settings.output_dir == tmp_path
