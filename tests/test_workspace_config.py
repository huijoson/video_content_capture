"""Serve settings are isolated from the established CLI settings."""

from pathlib import Path

import pytest

from video_content_capture.config import ConfigError, Settings
from video_content_capture.workspace.config import load_settings


@pytest.fixture(autouse=True)
def isolated_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    for name in (
        "ASSEMBLYAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "GEMINI_API_KEY",
        "GOOGLE_API_KEY",
        "VCC_GEMINI_TRANSLATION_MODEL",
        "VCC_GEMINI_QA_MODEL",
        "VCC_LIBRARY_DIR",
        "VCC_HOST",
        "VCC_PORT",
    ):
        monkeypatch.delenv(name, raising=False)


def test_settings_precedence_and_no_process_mutation(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text(
        'GEMINI_API_KEY="fake-dotenv-key"\nVCC_GEMINI_TRANSLATION_MODEL=dotenv-model\n'
        "VCC_GEMINI_QA_MODEL=dotenv-qa\nVCC_LIBRARY_DIR=dotenv-library\nVCC_PORT=9000\n"
    )
    dotenv = load_settings(tmp_path)
    assert dotenv.gemini_api_key.get_secret_value() == "fake-dotenv-key"
    assert dotenv.translation_model == "dotenv-model"
    assert dotenv.qa_model == "dotenv-qa"
    assert dotenv.library_dir == (tmp_path / "dotenv-library").resolve()
    assert dotenv.port == 9000
    monkeypatch.setenv("GEMINI_API_KEY", "fake-environment-key")
    monkeypatch.setenv("VCC_GEMINI_TRANSLATION_MODEL", "env-model")
    monkeypatch.setenv("VCC_PORT", "9001")
    environment = load_settings(tmp_path)
    assert environment.gemini_api_key.get_secret_value() == "fake-environment-key"
    assert environment.translation_model == "env-model"
    assert environment.port == 9001
    explicit = load_settings(tmp_path, translation_model="explicit-model", port=9002)
    assert explicit.translation_model == "explicit-model"
    assert explicit.port == 9002
    assert "fake-environment-key" not in repr(explicit)
    assert "fake-dotenv-key" not in repr(dotenv)
    assert Settings().assemblyai_api_key is None


def test_only_project_root_dotenv_and_no_google_fallback(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text("GEMINI_API_KEY=fake-parent-key\n")
    project = tmp_path / "project"
    project.mkdir()
    home = tmp_path / "home"
    home.mkdir()
    (home / ".env").write_text("GEMINI_API_KEY=fake-home-key\n")
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-google-key")
    monkeypatch.chdir(tmp_path)
    settings = load_settings(project)
    assert settings.gemini_api_key is None
    assert settings.translation_model == "gemini-3.5-flash-lite"
    assert settings.qa_model == "gemini-3.8-flash"
    assert settings.library_dir == (project / "outputs/library").resolve()
    assert settings.host == "127.0.0.1"
    assert settings.port == 8765


@pytest.mark.parametrize("host", ["0.0.0.0", "::", "192.168.1.1", "example.com"])
def test_non_loopback_binding_is_rejected(tmp_path: Path, host: str) -> None:
    with pytest.raises(ConfigError, match="loopback"):
        load_settings(tmp_path, host=host)


@pytest.mark.parametrize("host", ["127.0.0.1", "127.0.0.2", "::1", "localhost"])
def test_loopback_binding_is_accepted(tmp_path: Path, host: str) -> None:
    assert load_settings(tmp_path, host=host).host == host


def test_dotenv_no_interpolation_and_empty_environment_disables_key(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    (tmp_path / ".env").write_text("GEMINI_API_KEY=fake-file-key\n")
    monkeypatch.setenv("GEMINI_API_KEY", "")
    assert load_settings(tmp_path).gemini_api_key is None
    (tmp_path / ".env").write_text("VCC_GEMINI_QA_MODEL=${GOOGLE_API_KEY}\n")
    monkeypatch.setenv("GOOGLE_API_KEY", "fake-google-key")
    with pytest.raises(ConfigError):
        load_settings(tmp_path)


def test_config_errors_never_include_secret(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    secret = "fake-sentinel-secret"
    monkeypatch.setenv("GEMINI_API_KEY", secret)
    monkeypatch.setenv("VCC_PORT", secret)
    with pytest.raises(ConfigError) as caught:
        load_settings(tmp_path)
    assert secret not in str(caught.value)
    assert secret not in repr(caught.value)
