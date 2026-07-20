"""Explicitly opt-in live excerpt acceptance test.

This test is never eligible to call a provider unless BOTH conditions hold:

1. pytest is invoked with ``-m live``; and
2. ``VCC_ENABLE_LIVE=1`` is exported.

It also requires a user-created 30–60 second audio/video excerpt path and both
provider credentials. The default test suite therefore remains fully offline.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.live


def _require_explicit_live_opt_in(pytestconfig: pytest.Config) -> Path:
    markexpr = str(pytestconfig.getoption("markexpr"))
    if "live" not in markexpr or os.environ.get("VCC_ENABLE_LIVE") != "1":
        pytest.skip("requires both `pytest -m live` and VCC_ENABLE_LIVE=1")

    missing_names = [
        name
        for name in ("ASSEMBLYAI_API_KEY", "ANTHROPIC_API_KEY", "VCC_LIVE_EXCERPT_PATH")
        if not os.environ.get(name)
    ]
    assert not missing_names, f"missing required live-test variables: {', '.join(missing_names)}"

    excerpt = Path(os.environ["VCC_LIVE_EXCERPT_PATH"])
    assert excerpt.is_file(), "VCC_LIVE_EXCERPT_PATH must point to a 30–60 second excerpt"
    return excerpt


def test_live_excerpt_pipeline_and_resume(
    pytestconfig: pytest.Config,
    tmp_path: Path,
) -> None:
    excerpt = _require_explicit_live_opt_in(pytestconfig)
    output_dir = tmp_path / "live-output"

    base_command = [
        sys.executable,
        "-m",
        "video_content_capture",
        "run",
        str(excerpt),
        "--output-dir",
        str(output_dir),
        "--keep-audio",
    ]
    first = subprocess.run(
        base_command,
        capture_output=True,
        text=True,
        timeout=1_800,
        check=False,
    )
    assert first.returncode == 0, first.stderr

    raw_files = sorted(output_dir.rglob("*.raw/*.json"))
    assert raw_files, "live run must retain raw provider responses"
    mtimes = {path: path.stat().st_mtime_ns for path in raw_files}

    resumed = subprocess.run(
        [*base_command, "--resume"],
        capture_output=True,
        text=True,
        timeout=1_800,
        check=False,
    )
    assert resumed.returncode == 0, resumed.stderr
    assert {path: path.stat().st_mtime_ns for path in raw_files} == mtimes
