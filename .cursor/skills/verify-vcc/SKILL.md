---
name: verify-vcc
description: Drive the local YouTube workspace from `vcc serve` in a browser and prove what a user can see. Use when changing the workspace page, library, video load, one-click flow, or Q&A, and a unit test would not show the page.
---

# Verify the YouTube workspace

The user-facing app is the local workspace served by `vcc serve`. The page title is `YouTube 影片工作區`. The CLI commands `probe`, `transcribe`, `report`, and `run` write to an output directory the operator names. This harness drives the workspace page.

Run every command below from the repository root. The helper is `.cursor/skills/verify-vcc/scripts/control-vcc`. It needs `uv` on `PATH` (or at `~/.local/bin`) and `uv sync` already applied in this checkout, plus `ffmpeg` and `ffprobe` for any journey that transcodes. Google Chrome is the browser.

## Launch

`control-vcc launch` starts one workspace and waits until it is usable.

The server command is `uv run vcc serve` with `--host 127.0.0.1`, a free `--port`, a disposable `--project-dir`, and a disposable `--library-dir`. Both directories are created empty under the run home. The default run home is `.cursor/skills/verify-vcc/run`. Set `VCC_VERIFY_HOME` to a different directory to start a second instance. Launch refuses a second start in the same home while the first server is still alive.

The disposable project directory is not the repository. The repository `.env` is not read. The launch environment drops `GEMINI_API_KEY`, `GOOGLE_API_KEY`, `ASSEMBLYAI_API_KEY`, `ANTHROPIC_API_KEY`, and every `VCC_*` variable, so a shell key cannot turn Gemini on or point the library at `outputs/library`. To opt into Gemini, set `VCC_VERIFY_DOTENV` to a file you are allowed to use. Launch copies that file into the disposable project as a regular `.env` before the server starts.

Ready means all of the following are true. `GET /api/status` returns 200 and a version. `GET /` returns the workspace title. `GET /api/videos` returns a JSON list. `library.sqlite3` exists in the library directory. The server accepts connections only after library initialization, so a passing status check is past init.

Launch prints `url`, `pid`, `library_dir`, and `version`.

## Doctor

`control-vcc doctor` is read-only. Run it before driving whenever the page looks wrong.

It passes only when all of the following are true.

- The recorded pid is still the `vcc serve` process for this run's library directory.
- That process owns the port.
- `/api/status` reports the checkout version. The package version is `0.1.0` today.
- `/` contains `YouTube 影片工作區`.
- `/api/videos` is a JSON list.
- `library.sqlite3` is present.

Doctor prints `gemini_configured` from the status payload. Read that line. The presence of a key file is a different fact.

A failure names the broken check. Do not drive an instance this run did not start.

## Drive

Open the recorded URL in the Chrome this run started.

```bash
.cursor/skills/verify-vcc/scripts/control-vcc browser open
```

`browser open` waits until the title is `YouTube 影片工作區` and the service and library status lines have left their loading text. The window is 1280 by 900 CSS pixels, wider than the 60rem narrow layout, so the library, playback, and Q&A columns are all on the page.

Interact by role and accessible name.

```bash
.cursor/skills/verify-vcc/scripts/control-vcc browser click --role button --name "載入影片"
.cursor/skills/verify-vcc/scripts/control-vcc browser fill --role searchbox --name "搜尋標題" --value "季報"
.cursor/skills/verify-vcc/scripts/control-vcc browser text --id library-status
.cursor/skills/verify-vcc/scripts/control-vcc browser visible --id library-empty
.cursor/skills/verify-vcc/scripts/control-vcc browser wait --id import-status --text "影片已載入，請確認解析度後開始處理。"
.cursor/skills/verify-vcc/scripts/control-vcc browser wait --id flow-status --any "流程待處理" "流程排隊中" "流程處理中" --timeout 120
```

`fill` sets the control's value and dispatches `input` and `change`. Title search updates on that `input` event. There is no debounce. `wait` matches a substring and prints the text it found. `visible` exits non-zero when the element is hidden.

`control-vcc read /api/videos` is a GET of this instance only. Use it as a second view of stored rows. The helper rejects any path outside `/api/` and any method other than GET. Page actions go through `browser`.

The feature map is `.cursor/skills/verify-vcc/features/README.md`. Drive the entry point the feature file names. A proof that uses a different entry point does not cover the one you skipped.

## Evidence

Write proof under `.cursor/skills/verify-vcc/evidence/`, or under `VCC_VERIFY_EVIDENCE` when that variable is set. Relative `--path` values are joined to that directory. The path must stay inside it.

```bash
.cursor/skills/verify-vcc/scripts/control-vcc browser snapshot --aria --path library/empty.aria.txt
.cursor/skills/verify-vcc/scripts/control-vcc browser screenshot --path library/empty.png
```

The screenshot command scrolls to the top and captures the full page, so the heading `YouTube 影片工作區` stays in the image.

Proof standards:

- Exercise the page the user sees. Do not call an internal setter or a test-only endpoint.
- Capture the action and the resulting state. A final screenshot alone is not the proof.
- Pair a visible result with the stored result. For the library, that pair is the status line plus `control-vcc read /api/videos`.
- The ARIA snapshot and the screenshot both have to show the workspace title.
- Mocks are not part of this harness. A journey that needs YouTube or Gemini uses the real service. If the precondition is missing, stop and record the unmet precondition in the feature's Gotchas. Do not weaken the journey so it passes.
- `gemini_configured=false` is observed from doctor and from the status line `Gemini 未設定`. The absence of a key file is not that observation.
- A flow or query that fails still writes the status text the page shows. Do not treat a failure sentence as success.

## Cleanup

`control-vcc cleanup` stops the Chrome and the `vcc serve` process whose command lines contain this run's profile directory or library directory, then deletes the disposable project, library, and Chrome profile. It does not delete the evidence directory. It does not signal a process by name.

Run cleanup after a failed launch too, so a broken attempt does not keep the port.

Confirm the evidence files are still present after cleanup.

## Helpers

`.cursor/skills/verify-vcc/scripts/control-vcc` is executable. It runs `control_vcc.py` with `uv run` from the repository root.

```bash
.cursor/skills/verify-vcc/scripts/control-vcc launch
.cursor/skills/verify-vcc/scripts/control-vcc doctor
.cursor/skills/verify-vcc/scripts/control-vcc browser open
.cursor/skills/verify-vcc/scripts/control-vcc browser click --role button --name "載入影片"
.cursor/skills/verify-vcc/scripts/control-vcc browser fill --role searchbox --name "搜尋標題" --value "季報"
.cursor/skills/verify-vcc/scripts/control-vcc browser text --id library-status
.cursor/skills/verify-vcc/scripts/control-vcc browser visible --id library-empty
.cursor/skills/verify-vcc/scripts/control-vcc browser wait --id library-status --text "尚無影片。"
.cursor/skills/verify-vcc/scripts/control-vcc browser snapshot --aria --path library/empty.aria.txt
.cursor/skills/verify-vcc/scripts/control-vcc browser screenshot --path library/empty.png
.cursor/skills/verify-vcc/scripts/control-vcc read /api/videos
.cursor/skills/verify-vcc/scripts/control-vcc cleanup
```

When the workspace changes, run `/maintain-verification-skill` and update the feature map.
