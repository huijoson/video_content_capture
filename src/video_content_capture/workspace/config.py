"""Serve-only configuration; never search for or export dotenv credentials."""

from __future__ import annotations

import ipaddress
import os
import re
from dataclasses import dataclass, field
from pathlib import Path

from dotenv import dotenv_values
from pydantic import SecretStr

from video_content_capture.config import ConfigError


@dataclass(frozen=True)
class WorkspaceSettings:
    project_dir: Path
    library_dir: Path
    host: str = "127.0.0.1"
    port: int = 8765
    translation_model: str = "gemini-3.5-flash-lite"
    qa_model: str = "gemini-3.8-flash"
    gemini_api_key: SecretStr | None = field(default=None, repr=False)


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
        return os.environ.get(name, values.get(name) or default)

    raw_key = value("GEMINI_API_KEY", "").strip()
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
    for model in (translation, qa):
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,199}", model):
            raise ConfigError("Gemini model names must be nonempty identifiers")
    location = (
        library_dir
        if library_dir is not None
        else Path(value("VCC_LIBRARY_DIR", "outputs/library"))
    )
    if raw_key and any(
        raw_key in item for item in (str(location), translation, qa, effective_host)
    ):
        raise ConfigError("Credentials must not be used in public settings")
    return WorkspaceSettings(
        project_dir=root,
        library_dir=(root / location).absolute(),
        host=effective_host,
        port=effective_port,
        translation_model=translation,
        qa_model=qa,
        gemini_api_key=SecretStr(raw_key) if raw_key else None,
    )
