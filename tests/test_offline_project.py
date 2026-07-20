"""Offline project-contract tests for OpenSpec group 11.

These tests validate repository-owned fixtures, documentation coverage, uv's
actual development dependency group, live-test isolation, and secret-free test
artifacts. They never inspect real environment secret values or make network
requests.
"""

from __future__ import annotations

import json
import tomllib
import wave
from pathlib import Path

import pytest
from pydantic import ValidationError

from video_content_capture.reporting.base import StructuredReport

PROJECT_ROOT = Path(__file__).resolve().parents[1]
FIXTURES = PROJECT_ROOT / "tests" / "fixtures"


def test_pyproject_uses_uv_dev_dependency_group() -> None:
    data = tomllib.loads((PROJECT_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    dev = data["dependency-groups"]["dev"]
    names = {entry.split("[", 1)[0].split(">", 1)[0].split("=", 1)[0] for entry in dev}
    assert {"pytest", "pytest-mock", "ruff", "mypy"} <= names
    assert "dev" not in data.get("project", {}).get("optional-dependencies", {})


def test_readme_documents_required_operator_contracts() -> None:
    readme = (PROJECT_ROOT / "README.md").read_text(encoding="utf-8")

    required_text = (
        "uv sync --dev",
        "ffmpeg -version",
        "ffprobe -version",
        "ASSEMBLYAI_API_KEY",
        "ANTHROPIC_API_KEY",
        "vcc probe",
        "vcc transcribe",
        "vcc report",
        "vcc run",
        "python -m video_content_capture",
        "--output-dir",
        "--language",
        "--min-speakers",
        "--max-speakers",
        "--resume",
        "--force",
        "--keep-audio",
        "視野環球財經robots_07-19-2026 22-11-19_1.MP4",
        "34:20",
        "1.545 GB",
        "claude-opus-4-8",
        "隱私",
        "成本",
        "來源索引",
        "講者觀點",
        "Ctrl-C",
        "pytest -m live",
        "VCC_ENABLE_LIVE",
    )
    for text in required_text:
        assert text in readme, f"README missing {text!r}"

    for code in (0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 130):
        assert f"| {code} |" in readme


def test_fixture_provenance_and_license_note_are_present() -> None:
    text = (FIXTURES / "README.md").read_text(encoding="utf-8")
    assert "synthetic" in text.lower()
    assert "public domain" in text.lower()
    assert "不含" in text
    assert "API key" in text


def test_repository_owned_synthetic_wav_is_small_and_valid() -> None:
    path = FIXTURES / "media" / "synthetic-tone.wav"
    assert path.stat().st_size < 100_000

    with wave.open(str(path), "rb") as wav:
        assert wav.getnchannels() == 1
        assert wav.getsampwidth() == 2
        assert wav.getframerate() == 8_000
        assert wav.getnframes() == 8_000


@pytest.mark.parametrize(
    "relative_path",
    [
        "providers/assemblyai_multi_speaker.json",
        "providers/assemblyai_malformed.json",
        "providers/claude_report_valid.json",
        "providers/claude_report_invalid.json",
    ],
)
def test_mock_provider_fixtures_are_parseable_json(relative_path: str) -> None:
    data = json.loads((FIXTURES / relative_path).read_text(encoding="utf-8"))
    assert isinstance(data, dict)


def test_valid_claude_fixture_matches_structured_output_schema() -> None:
    data = json.loads(
        (FIXTURES / "providers" / "claude_report_valid.json").read_text(encoding="utf-8")
    )
    report = StructuredReport.model_validate(data)
    assert report.source_index.entries == ["s0001", "s0002"]


def test_invalid_claude_fixture_is_rejected_by_structured_output_schema() -> None:
    data = json.loads(
        (FIXTURES / "providers" / "claude_report_invalid.json").read_text(encoding="utf-8")
    )
    with pytest.raises(ValidationError):
        StructuredReport.model_validate(data)


def test_synthetic_media_fixture_is_not_gitignored() -> None:
    ignore_rules = (PROJECT_ROOT / ".gitignore").read_text(encoding="utf-8")
    assert "!tests/fixtures/media/synthetic-tone.wav" in ignore_rules


def test_live_acceptance_module_requires_marker_and_explicit_opt_in() -> None:
    source = (PROJECT_ROOT / "tests" / "test_live_acceptance.py").read_text(encoding="utf-8")
    assert "pytest.mark.live" in source
    assert "VCC_ENABLE_LIVE" in source
    assert 'getoption("markexpr")' in source
    assert "VCC_LIVE_EXCERPT_PATH" in source


def test_owned_fixtures_contain_no_secret_like_values() -> None:
    forbidden = (
        "sk-ant-",
        "assembly-secret",
        "anthropic-secret",
        "test-anthropic-key",
        "test-assemblyai-key",
    )
    for path in FIXTURES.rglob("*"):
        if not path.is_file():
            continue
        blob = path.read_bytes()
        text = blob.decode("utf-8", errors="ignore")
        for value in forbidden:
            assert value not in text, f"secret-like value {value!r} found in {path}"
