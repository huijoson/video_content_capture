# AGENTS.md

## Agent skills

### Issue tracker

Issues are tracked in GitHub Issues (huijoson/video_content_capture) via the `gh` CLI. See `docs/agents/issue-tracker.md`.

### Triage labels

Default labels: `needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`. See `docs/agents/triage-labels.md`.

### Domain docs

Single-context: glossary in `CONTEXT.md`, ADRs in `docs/adr/`. See `docs/agents/domain.md`.

## Cursor Cloud specific instructions

- The project requires Python 3.13. System `python3` is 3.12. Use `uv` (`uv python install 3.13`, then `uv run`). `uv` is on the login-shell PATH at `~/.local/bin/uv` and `/usr/local/bin/uv`.
- Dependencies: `uv sync --frozen --dev` from the repository root. This matches `uv.lock` and installs the dev group (pytest, pytest-mock, Ruff, mypy).
- `ffmpeg` and `ffprobe` are already on PATH and include libx264 and AAC. Compatible previews need that encoder.
- Checks, from the repository root: `uv run ruff check .`, `uv run ruff format --check .`, `uv run mypy src`, `uv run pytest -q`. The default suite is offline and does not call providers. Live tests (`pytest -m live`) are opt-in and make paid requests.
- Local workspace: `uv run vcc serve --project-dir . --host 127.0.0.1 --port 8765`, then open `http://127.0.0.1:8765`. The server binds loopback only. A second start is safe when `/api/status` already responds.
- `vcc probe` needs no API key. `ASSEMBLYAI_API_KEY`, `ANTHROPIC_API_KEY`, and `GEMINI_API_KEY` are optional. Translation and Q&A stay disabled until `GEMINI_API_KEY` is set in the project-root `.env` and the server is restarted. `serve` reads only that `.env`.
- Video sites, YouTube included, can refuse metadata and downloads from cloud network addresses (`source_blocked`). Probe, the offline tests, and the workspace shell do not need a video site.
- The MLX Whisper extra is Apple Silicon only and is not installed on Linux. Use the AssemblyAI path or offline fixtures here.
