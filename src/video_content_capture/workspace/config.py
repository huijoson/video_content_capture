"""Serve-only configuration; never search for or export dotenv credentials."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal, get_args

from dotenv import dotenv_values
from pydantic import SecretStr

from video_content_capture.config import ConfigError

ASRBackend = Literal["gemini", "mlx"]
ASR_BACKENDS: tuple[ASRBackend, ...] = get_args(ASRBackend)


@dataclass(frozen=True)
class WorkspaceSettings:
    project_dir: Path
    library_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    translation_model: str = "gemini-3.5-flash-lite"
    qa_model: str = "gemini-3.8-flash"
    asr_backend: ASRBackend = "gemini"
    asr_model: str = "gemini-3.8-flash"
    gemini_api_key: SecretStr | None = field(default=None, repr=False)
    # Where the key above was read from, and whether an environment variable won over a
    # different value in the project .env. `load_settings` stays a pure read; callers do
    # the announcing, so tests and the app share one answer.
    gemini_key_source: str | None = None
    gemini_key_shadowed: bool = False


def validate_binding(host: str, port: int) -> None:
    if host != "localhost":
        try:
            loopback = ipaddress.ip_address(host).is_loopback
        except ValueError:
            loopback = False
        if not loopback:
            raise ConfigError("serve host must be a loopback IP address or localhost")
    if not 1 <= port <= 65535:
        raise ConfigError("serve port must be between 1 and 65535")


def load_settings(
    project_dir: Path,
    *,
    host: str | None = None,
    port: int | None = None,
    library_dir: Path | None = None,
    translation_model: str | None = None,
    qa_model: str | None = None,
) -> WorkspaceSettings:
    root = project_dir.resolve()
    if not root.is_dir():
        raise ConfigError("serve project directory must exist")
    dotenv_path = root / ".env"
    if dotenv_path.is_symlink():
        raise ConfigError("serve .env must not be a symbolic link")
    try:
        values = dotenv_values(dotenv_path, interpolate=False) if dotenv_path.exists() else {}
    except (OSError, UnicodeError):
        raise ConfigError("Cannot read project .env") from None

    def value(name: str, default: str) -> str:
        """An environment variable wins over `.env`; one set to "" wins and clears it."""
        return os.environ.get(name, values.get(name) or default)

    raw_key = value("GEMINI_API_KEY", "").strip()
    environment_key = (os.environ.get("GEMINI_API_KEY") or "").strip()
    file_key = (values.get("GEMINI_API_KEY") or "").strip()
    # `value` reads the environment first, so the effective key comes from the environment
    # whenever it holds a value. An environment variable set to "" wins too and clears the
    # key outright, which is why a shadowing environment entry is reported either way.
    key_source = ("environment" if environment_key else ".env") if raw_key else None
    key_shadowed = bool(
        file_key
        and "GEMINI_API_KEY" in os.environ
        and (not environment_key or environment_key != file_key)
    )
    effective_host = host if host is not None else value("VCC_HOST", "127.0.0.1")
    try:
        effective_port = port if port is not None else int(value("VCC_PORT", "8765"))
    except ValueError:
        raise ConfigError("VCC_PORT must be an integer") from None
    validate_binding(effective_host, effective_port)
    translation = (
        translation_model
        if translation_model is not None
        else value("VCC_GEMINI_TRANSLATION_MODEL", "gemini-3.5-flash-lite")
    )
    qa = qa_model if qa_model is not None else value("VCC_GEMINI_QA_MODEL", "gemini-3.8-flash")
    backend = value("VCC_ASR_BACKEND", "gemini")
    if backend not in ASR_BACKENDS:
        raise ConfigError("VCC_ASR_BACKEND must be gemini or mlx")
    asr_model = value("VCC_GEMINI_ASR_MODEL", "gemini-3.8-flash")
    for model in (translation, qa, asr_model):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", model):
            raise ConfigError("Gemini model names must be nonempty identifiers")
    location = (
        library_dir
        if library_dir is not None
        else Path(value("VCC_LIBRARY_DIR", "outputs/library"))
    )
    if raw_key and any(
        raw_key in item for item in (str(location), translation, qa, asr_model, effective_host)
    ):
        raise ConfigError("Credentials must not be used in public settings")
    return WorkspaceSettings(
        project_dir=root,
        library_dir=(root / location).absolute(),
        host=effective_host,
        port=effective_port,
        translation_model=translation,
        qa_model=qa,
        asr_backend=backend,
        asr_model=asr_model,
        gemini_api_key=SecretStr(raw_key) if raw_key else None,
        gemini_key_source=key_source,
        gemini_key_shadowed=key_shadowed,
    )
