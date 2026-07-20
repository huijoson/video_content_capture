# Context Handoff Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Install a personal Claude Code context-handoff Skill and local gate that warns at 75%, automatically requests one bounded handoff at 82%, and proves duplicate Hook execution cannot amplify Token use.

**Architecture:** A local status-line script writes atomic per-Session state, and a local Hook gate reads that state and emits at most one compact `additionalContext` payload per Session. A bounded Skill creates the checkpoint, while a standalone self-test exercises the runtime 1,001 times without launching Claude Code or calling any API.

**Tech Stack:** Python 3.13 standard library, Claude Code 2.1.215 settings/Hooks/Skills, JSON, Markdown.

## Global Constraints

- Install personal files only under `/Users/yuhan/.claude`; checkpoint output remains project-relative under `docs/checkpoints`.
- Warning threshold is exactly `75`; automatic handoff threshold is exactly `82`.
- Set `DISABLE_AUTO_COMPACT=1`; never set `DISABLE_COMPACT`.
- Each Session may emit at most one automatic handoff `additionalContext` payload.
- Retain the `.triggered` marker after completion as the permanent Session deduplication record.
- A `Stop` event with `stop_hook_active: true` must never trigger handoff.
- Injected `additionalContext` must be at most 1,200 UTF-8 bytes.
- Combined Skill `description` plus `when_to_use` must be at most 500 characters.
- The complete `SKILL.md` file must be at most 6,000 UTF-8 bytes.
- Runtime scripts may use only the Python standard library and must not import networking, API-client, or model-provider packages.
- The Skill must disallow `Agent` and `Workflow` and must not perform broad repository or transcript exploration.
- Do not run a real Claude Session as part of automated verification.
- Preserve every existing setting not explicitly added by this plan.
- The target global files are outside Git and the current workspace is not a Git repository. Use the settings backup in Task 3 as the rollback checkpoint; do not initialize a repository or create commits.

---

## File Map

| Path | Action | Responsibility |
| --- | --- | --- |
| `/Users/yuhan/.claude/context-handoff/statusline.py` | Create | Parse status-line JSON, atomically persist Session context state, and render context/handoff status. |
| `/Users/yuhan/.claude/context-handoff/gate.py` | Create | Evaluate Stop/UserPromptSubmit events and emit exactly one bounded handoff instruction per Session. |
| `/Users/yuhan/.claude/context-handoff/selftest.py` | Create | Run deterministic runtime, Skill, settings, compile, and no-network checks without calling Claude. |
| `/Users/yuhan/.claude/skills/context-handoff/SKILL.md` | Create | Define the bounded checkpoint workflow and evidence budget. |
| `/Users/yuhan/.claude/settings.json` | Modify | Add environment variables, status line, Stop Hook, and UserPromptSubmit Hook while preserving existing settings. |
| `/Users/yuhan/.claude/settings.json.context-handoff-backup-20260720` | Create once | Preserve the exact pre-installation global settings for rollback. |

## Runtime Interfaces

`statusline.py` produces these functions for `gate.py` and `selftest.py`:

- `safe_session_id(value: object) -> str`
- `parse_percentage(value: object) -> int`
- `get_threshold(env: Mapping[str, str], name: str, default: int) -> int`
- `resolve_home(home: Path | None = None) -> Path`
- `state_paths(session_id: object, home: Path | None = None) -> dict[str, Path]`
- `build_state(data: dict[str, object], *, home: Path | None = None, env: Mapping[str, str] | None = None, now: float | None = None) -> dict[str, object]`
- `write_state(state: dict[str, object], *, home: Path | None = None) -> None`
- `render_status(state: dict[str, object]) -> str`

`gate.py` produces these functions for `selftest.py`:

- `build_message(percentage: int, session_id: str, state_key: str) -> str`
- `evaluate_gate(hook_input: dict[str, object], *, home: Path | None = None, env: Mapping[str, str] | None = None, now: float | None = None) -> dict[str, object] | None`

---

### Task 1: Build and test the local status detector and one-shot gate

**Files:**
- Create: `/Users/yuhan/.claude/context-handoff/selftest.py`
- Create: `/Users/yuhan/.claude/context-handoff/statusline.py`
- Create: `/Users/yuhan/.claude/context-handoff/gate.py`

**Interfaces:**
- Consumes: Claude Code status-line JSON and Hook JSON; `HOME`; `CLAUDE_HANDOFF_WARNING_THRESHOLD`; `CLAUDE_HANDOFF_THRESHOLD`.
- Produces: Atomic state files under `$HOME/.claude/context-handoff-state`, terminal status text, and one optional Hook JSON object.

- [ ] **Step 1: Create the runtime and state directories**

Run:

```bash
mkdir -p /Users/yuhan/.claude/context-handoff
mkdir -p /Users/yuhan/.claude/context-handoff-state
```

Expected: both commands exit `0` with no output.

- [ ] **Step 2: Write the failing consolidated self-test**

Create `/Users/yuhan/.claude/context-handoff/selftest.py` with:

```python
#!/usr/bin/env python3

from __future__ import annotations

import argparse
import ast
import importlib.util
import json
import os
import py_compile
import subprocess
import sys
import tempfile
from pathlib import Path
from types import ModuleType

BASE_DIR = Path(__file__).resolve().parent
CLAUDE_DIR = BASE_DIR.parent
STATUSLINE_PATH = BASE_DIR / "statusline.py"
GATE_PATH = BASE_DIR / "gate.py"
SKILL_PATH = CLAUDE_DIR / "skills" / "context-handoff" / "SKILL.md"
SETTINGS_PATH = CLAUDE_DIR / "settings.json"

BANNED_RUNTIME_IMPORTS = {
    "aiohttp",
    "anthropic",
    "httpx",
    "openai",
    "requests",
    "socket",
    "urllib",
}


def require(condition: bool, message: str) -> None:
    if not condition:
        raise AssertionError(message)


def load_module(name: str, path: Path) -> ModuleType:
    require(path.is_file(), f"Missing required file: {path}")
    spec = importlib.util.spec_from_file_location(name, path)
    require(spec is not None and spec.loader is not None, f"Cannot load {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def mock_status(session_id: str, percentage: int) -> dict[str, object]:
    return {
        "session_id": session_id,
        "cwd": "/tmp/test-project",
        "workspace": {
            "current_dir": "/tmp/test-project",
            "project_dir": "/tmp/test-project",
        },
        "transcript_path": "/tmp/test-transcript.jsonl",
        "context_window": {"used_percentage": percentage},
    }


def run_invalid_json_check(path: Path, home: Path) -> None:
    environment = os.environ.copy()
    environment["HOME"] = str(home)
    result = subprocess.run(
        [sys.executable, str(path)],
        input="{not-valid-json",
        text=True,
        capture_output=True,
        env=environment,
        check=False,
    )
    require(result.returncode == 0, f"{path.name} returned {result.returncode}")
    require(result.stderr == "", f"{path.name} wrote a traceback: {result.stderr}")


def assert_no_banned_imports(path: Path) -> None:
    source = path.read_text(encoding="utf-8")
    tree = ast.parse(source, filename=str(path))
    imported_roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported_roots.update(alias.name.split(".", 1)[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported_roots.add(node.module.split(".", 1)[0])
    banned = imported_roots & BANNED_RUNTIME_IMPORTS
    require(not banned, f"{path.name} imports banned modules: {sorted(banned)}")


def run_runtime_tests() -> dict[str, int]:
    if str(BASE_DIR) not in sys.path:
        sys.path.insert(0, str(BASE_DIR))

    statusline = load_module("statusline", STATUSLINE_PATH)
    gate = load_module("gate", GATE_PATH)
    env = {
        "CLAUDE_HANDOFF_WARNING_THRESHOLD": "75",
        "CLAUDE_HANDOFF_THRESHOLD": "82",
    }

    with tempfile.TemporaryDirectory(prefix="context-handoff-test-") as temp_dir:
        home = Path(temp_dir)

        cases = (
            ("normal-session", 74, "[CONTEXT]"),
            ("warning-session", 75, "[CONTEXT WARNING]"),
            ("required-session", 82, "[HANDOFF REQUIRED]"),
        )
        for session_id, percentage, expected_label in cases:
            state = statusline.build_state(
                mock_status(session_id, percentage),
                home=home,
                env=env,
                now=1_721_440_000,
            )
            statusline.write_state(state, home=home)
            rendered = statusline.render_status(state)
            require(expected_label in rendered, f"Missing {expected_label}: {rendered}")

        below_state = statusline.build_state(
            mock_status("below-session", 81),
            home=home,
            env=env,
            now=1_721_440_001,
        )
        statusline.write_state(below_state, home=home)
        below = gate.evaluate_gate(
            {
                "session_id": "below-session",
                "hook_event_name": "Stop",
                "stop_hook_active": False,
            },
            home=home,
            env=env,
            now=1_721_440_001,
        )
        require(below is None, "Gate emitted below the handoff threshold")

        trigger_state = statusline.build_state(
            mock_status("trigger-session", 82),
            home=home,
            env=env,
            now=1_721_440_002,
        )
        statusline.write_state(trigger_state, home=home)
        first = gate.evaluate_gate(
            {
                "session_id": "trigger-session",
                "hook_event_name": "Stop",
                "stop_hook_active": False,
            },
            home=home,
            env=env,
            now=1_721_440_002,
        )
        require(first is not None, "First eligible event did not emit")
        hook_output = first.get("hookSpecificOutput", {})
        require(isinstance(hook_output, dict), "hookSpecificOutput is not an object")
        require(hook_output.get("hookEventName") == "Stop", "Wrong Hook event name")
        payload = hook_output.get("additionalContext")
        require(isinstance(payload, str), "additionalContext is not a string")
        payload_bytes = len(payload.encode("utf-8"))
        require(payload_bytes <= 1_200, f"Payload is {payload_bytes} bytes")

        pending_state = statusline.build_state(
            mock_status("trigger-session", 82),
            home=home,
            env=env,
            now=1_721_440_002,
        )
        require(
            "[HANDOFF PENDING]" in statusline.render_status(pending_state),
            "Triggered Session did not render as pending",
        )

        duplicate_emissions = 0
        for index in range(1_000):
            duplicate = gate.evaluate_gate(
                {
                    "session_id": "trigger-session",
                    "hook_event_name": "Stop" if index % 2 == 0 else "UserPromptSubmit",
                    "stop_hook_active": False,
                },
                home=home,
                env=env,
                now=1_721_440_003 + index,
            )
            duplicate_emissions += int(duplicate is not None)
        require(duplicate_emissions == 0, "Duplicate gate emissions were observed")

        active_state = statusline.build_state(
            mock_status("active-stop-session", 90),
            home=home,
            env=env,
            now=1_721_441_100,
        )
        statusline.write_state(active_state, home=home)
        active_stop = gate.evaluate_gate(
            {
                "session_id": "active-stop-session",
                "hook_event_name": "Stop",
                "stop_hook_active": True,
            },
            home=home,
            env=env,
            now=1_721_441_100,
        )
        require(active_stop is None, "stop_hook_active emitted a handoff")

        done_state = statusline.build_state(
            mock_status("done-session", 95),
            home=home,
            env=env,
            now=1_721_441_101,
        )
        statusline.write_state(done_state, home=home)
        done_paths = statusline.state_paths("done-session", home)
        done_paths["done"].write_text("complete\n", encoding="utf-8")
        ready_state = statusline.build_state(
            mock_status("done-session", 95),
            home=home,
            env=env,
            now=1_721_441_101,
        )
        require(
            "[HANDOFF READY]" in statusline.render_status(ready_state),
            "Completed Session did not render as ready",
        )
        done_output = gate.evaluate_gate(
            {
                "session_id": "done-session",
                "hook_event_name": "UserPromptSubmit",
            },
            home=home,
            env=env,
            now=1_721_441_101,
        )
        require(done_output is None, "Completed Session emitted a handoff")

        run_invalid_json_check(STATUSLINE_PATH, home)
        run_invalid_json_check(GATE_PATH, home)

    assert_no_banned_imports(STATUSLINE_PATH)
    assert_no_banned_imports(GATE_PATH)

    return {
        "gate_evaluations": 1_001,
        "automatic_emissions": 1,
        "duplicate_emissions": duplicate_emissions,
        "payload_bytes": payload_bytes,
        "banned_imports": 0,
    }


def frontmatter_value(frontmatter: str, key: str) -> str:
    prefix = f"{key}:"
    for line in frontmatter.splitlines():
        if line.startswith(prefix):
            return line[len(prefix) :].strip()
    raise AssertionError(f"Missing frontmatter key: {key}")


def run_skill_tests() -> dict[str, int]:
    require(SKILL_PATH.is_file(), f"Missing required file: {SKILL_PATH}")
    source = SKILL_PATH.read_text(encoding="utf-8")
    source_bytes = len(source.encode("utf-8"))
    require(source_bytes <= 6_000, f"SKILL.md is {source_bytes} bytes")
    require(source.startswith("---\n"), "SKILL.md has no YAML frontmatter")
    parts = source.split("---", 2)
    require(len(parts) == 3, "SKILL.md frontmatter is not closed")
    frontmatter = parts[1]
    description = frontmatter_value(frontmatter, "description")
    when_to_use = frontmatter_value(frontmatter, "when_to_use")
    metadata_chars = len(description) + len(when_to_use)
    require(metadata_chars <= 500, f"Skill metadata is {metadata_chars} characters")
    require("name: context-handoff" in frontmatter, "Wrong Skill name")
    require("user-invocable: true" in frontmatter, "Skill is not user-invocable")
    require(
        "disallowed-tools: Agent Workflow" in frontmatter,
        "Agent and Workflow are not disallowed",
    )

    required_phrases = (
        "Do not run `/compact` or `/clear`.",
        "Do not invoke Agent or Workflow.",
        "at most two bounded transcript excerpts",
        "Run at most one focused test command",
        "docs/checkpoints/current-handoff.md",
        "retain the matching `.triggered` marker",
        "Do not guess a Session ID",
    )
    for phrase in required_phrases:
        require(phrase in source, f"Missing Skill safety rule: {phrase}")

    return {
        "skill_bytes": source_bytes,
        "skill_metadata_chars": metadata_chars,
    }


def run_settings_tests() -> dict[str, int]:
    settings = json.loads(SETTINGS_PATH.read_text(encoding="utf-8"))
    env = settings.get("env", {})
    require(env.get("DISABLE_AUTO_COMPACT") == "1", "Auto compact is not disabled")
    require("DISABLE_COMPACT" not in env, "Manual compact was disabled")
    require(env.get("CLAUDE_HANDOFF_WARNING_THRESHOLD") == "75", "Wrong warning threshold")
    require(env.get("CLAUDE_HANDOFF_THRESHOLD") == "82", "Wrong handoff threshold")

    status_line = settings.get("statusLine", {})
    require(status_line.get("type") == "command", "Wrong statusLine type")
    require(
        status_line.get("command") == "python3 ~/.claude/context-handoff/statusline.py",
        "Wrong statusLine command",
    )

    hooks = settings.get("hooks", {})
    expected_command = "python3 ~/.claude/context-handoff/gate.py"
    for event in ("Stop", "UserPromptSubmit"):
        groups = hooks.get(event)
        require(isinstance(groups, list) and len(groups) == 1, f"Wrong {event} Hook groups")
        handlers = groups[0].get("hooks", [])
        require(isinstance(handlers, list) and len(handlers) == 1, f"Wrong {event} handlers")
        handler = handlers[0]
        require(handler.get("type") == "command", f"Wrong {event} Hook type")
        require(handler.get("command") == expected_command, f"Wrong {event} command")
        require(handler.get("timeout") == 5, f"Wrong {event} timeout")

    return {"settings_json": 1}


def run_compile_tests() -> dict[str, int]:
    for path in (STATUSLINE_PATH, GATE_PATH, Path(__file__).resolve()):
        py_compile.compile(str(path), doraise=True)
    return {"python_compile": 1}


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "section",
        nargs="?",
        choices=("runtime", "skill", "settings", "all"),
        default="all",
    )
    args = parser.parse_args()

    metrics: dict[str, int] = {}
    if args.section in {"runtime", "all"}:
        metrics.update(run_runtime_tests())
    if args.section in {"skill", "all"}:
        metrics.update(run_skill_tests())
    if args.section in {"settings", "all"}:
        metrics.update(run_settings_tests())
    if args.section == "all":
        metrics.update(run_compile_tests())

    print("Context handoff self-test: PASS")
    labels = (
        ("gate_evaluations", "Gate evaluations"),
        ("automatic_emissions", "Automatic emissions"),
        ("duplicate_emissions", "Duplicate emissions"),
        ("payload_bytes", "Injected payload bytes"),
        ("banned_imports", "Banned runtime imports"),
        ("skill_bytes", "SKILL.md bytes"),
        ("skill_metadata_chars", "Skill metadata characters"),
        ("settings_json", "Settings JSON"),
        ("python_compile", "Python compile"),
    )
    for key, label in labels:
        if key in metrics:
            value = "PASS" if key in {"settings_json", "python_compile"} else metrics[key]
            print(f"{label}: {value}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 3: Run the runtime test and verify it fails before implementation**

Run:

```bash
python3 /Users/yuhan/.claude/context-handoff/selftest.py runtime
```

Expected: FAIL with `AssertionError: Missing required file: /Users/yuhan/.claude/context-handoff/statusline.py`.

- [ ] **Step 4: Implement the status-line state writer and renderer**

Create `/Users/yuhan/.claude/context-handoff/statusline.py` with:

```python
#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from time import time

RESET = "\033[0m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
RED = "\033[31m"
CYAN = "\033[36m"


def safe_session_id(value: object) -> str:
    raw = str(value or "unknown-session")
    safe = "".join(
        character
        if character.isascii() and (character.isalnum() or character in "-_.")
        else "_"
        for character in raw
    )
    return (safe[:200] or "unknown-session")


def parse_percentage(value: object) -> int:
    try:
        percentage = int(float(value or 0))
    except (TypeError, ValueError):
        return 0
    return max(0, min(100, percentage))


def get_threshold(env: Mapping[str, str], name: str, default: int) -> int:
    try:
        value = int(env.get(name, str(default)))
    except (TypeError, ValueError):
        return default
    return max(0, min(100, value))


def resolve_home(home: Path | None = None) -> Path:
    if home is not None:
        return home
    return Path(os.environ.get("HOME") or Path.home())


def state_paths(session_id: object, home: Path | None = None) -> dict[str, Path]:
    state_dir = resolve_home(home) / ".claude" / "context-handoff-state"
    state_key = safe_session_id(session_id)
    return {
        "dir": state_dir,
        "state": state_dir / f"{state_key}.json",
        "triggered": state_dir / f"{state_key}.triggered",
        "done": state_dir / f"{state_key}.done",
    }


def build_state(
    data: dict[str, object],
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
) -> dict[str, object]:
    environment = os.environ if env is None else env
    context = data.get("context_window")
    if not isinstance(context, dict):
        context = {}
    workspace = data.get("workspace")
    if not isinstance(workspace, dict):
        workspace = {}

    session_id = str(data.get("session_id") or "unknown-session")
    percentage = parse_percentage(context.get("used_percentage"))
    warning_threshold = get_threshold(
        environment,
        "CLAUDE_HANDOFF_WARNING_THRESHOLD",
        75,
    )
    handoff_threshold = get_threshold(
        environment,
        "CLAUDE_HANDOFF_THRESHOLD",
        82,
    )
    paths = state_paths(session_id, home)
    triggered = paths["triggered"].exists()
    done = paths["done"].exists()

    project_dir = (
        workspace.get("project_dir")
        or workspace.get("current_dir")
        or data.get("cwd")
        or ""
    )

    return {
        "session_id": session_id,
        "state_key": safe_session_id(session_id),
        "percentage": percentage,
        "project_dir": str(project_dir),
        "transcript_path": str(data.get("transcript_path") or ""),
        "updated_at": int(time() if now is None else now),
        "warning_threshold": warning_threshold,
        "handoff_threshold": handoff_threshold,
        "triggered": triggered,
        "done": done,
        "handoff_required": percentage >= handoff_threshold and not triggered and not done,
    }


def write_state(state: dict[str, object], *, home: Path | None = None) -> None:
    paths = state_paths(state.get("session_id"), home)
    paths["dir"].mkdir(parents=True, exist_ok=True)
    temporary = paths["dir"] / f"{paths['state'].name}.{os.getpid()}.tmp"
    try:
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        temporary.replace(paths["state"])
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def render_status(state: dict[str, object]) -> str:
    percentage = parse_percentage(state.get("percentage"))
    warning_threshold = parse_percentage(state.get("warning_threshold"))
    handoff_threshold = parse_percentage(state.get("handoff_threshold"))

    if bool(state.get("done")):
        return f"{CYAN}[HANDOFF READY]{RESET} Context {percentage}% | /clear when ready"
    if bool(state.get("triggered")):
        return (
            f"{RED}[HANDOFF PENDING]{RESET} Context {percentage}% | "
            "run /context-handoff if needed"
        )
    if percentage >= handoff_threshold:
        return f"{RED}[HANDOFF REQUIRED]{RESET} Context {percentage}% | preparing checkpoint"
    if percentage >= warning_threshold:
        return f"{YELLOW}[CONTEXT WARNING]{RESET} Context {percentage}% | handoff soon"
    return f"{GREEN}[CONTEXT]{RESET} {percentage}%"


def main() -> int:
    try:
        data = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, TypeError):
        return 0
    if not isinstance(data, dict):
        return 0

    state = build_state(data)
    try:
        write_state(state)
    except OSError:
        pass
    print(render_status(state))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 5: Run the runtime test and verify the next missing boundary**

Run:

```bash
python3 /Users/yuhan/.claude/context-handoff/selftest.py runtime
```

Expected: FAIL with `AssertionError: Missing required file: /Users/yuhan/.claude/context-handoff/gate.py`.

- [ ] **Step 6: Implement the one-shot Hook gate**

Create `/Users/yuhan/.claude/context-handoff/gate.py` with:

```python
#!/usr/bin/env python3

from __future__ import annotations

import json
import os
import sys
from collections.abc import Mapping
from pathlib import Path
from time import time

import statusline

SUPPORTED_EVENTS = {"Stop", "UserPromptSubmit"}
MAX_ADDITIONAL_CONTEXT_BYTES = 1_200


def build_message(percentage: int, session_id: str, state_key: str) -> str:
    return "\n".join(
        (
            "CONTEXT_HANDOFF_REQUIRED",
            f"Session policy: context usage is {percentage}% and a handoff is required.",
            f"Session ID: {session_id}",
            "Invoke the context-handoff skill now. Stop feature implementation and create and validate docs/checkpoints/current-handoff.md.",
            "Do not run /compact or /clear, start another task, resume implementation, invoke subagents, or run workflows.",
            f"After validation, create ~/.claude/context-handoff-state/{state_key}.done, retain the .triggered marker, end the turn, and wait for user review.",
        )
    )


def evaluate_gate(
    hook_input: dict[str, object],
    *,
    home: Path | None = None,
    env: Mapping[str, str] | None = None,
    now: float | None = None,
) -> dict[str, object] | None:
    environment = os.environ if env is None else env
    event_name = str(hook_input.get("hook_event_name") or "")
    if event_name not in SUPPORTED_EVENTS:
        return None

    session_id = str(hook_input.get("session_id") or "unknown-session")
    state_key = statusline.safe_session_id(session_id)
    paths = statusline.state_paths(session_id, home)

    if paths["done"].exists() or paths["triggered"].exists():
        return None
    if not paths["state"].is_file():
        return None
    if event_name == "Stop" and hook_input.get("stop_hook_active") is True:
        return None

    try:
        state = json.loads(paths["state"].read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError, TypeError):
        return None
    if not isinstance(state, dict):
        return None
    if str(state.get("session_id") or "") != session_id:
        return None

    percentage = statusline.parse_percentage(state.get("percentage"))
    threshold = statusline.get_threshold(
        environment,
        "CLAUDE_HANDOFF_THRESHOLD",
        82,
    )
    if percentage < threshold:
        return None

    message = build_message(percentage, session_id, state_key)
    if len(message.encode("utf-8")) > MAX_ADDITIONAL_CONTEXT_BYTES:
        return None

    marker = {
        "session_id": session_id,
        "event": event_name,
        "percentage": percentage,
        "triggered_at": int(time() if now is None else now),
    }
    try:
        paths["dir"].mkdir(parents=True, exist_ok=True)
        with paths["triggered"].open("x", encoding="utf-8") as handle:
            json.dump(marker, handle, ensure_ascii=False, indent=2)
            handle.write("\n")
    except (FileExistsError, OSError):
        return None

    return {
        "hookSpecificOutput": {
            "hookEventName": event_name,
            "additionalContext": message,
        }
    }


def main() -> int:
    try:
        hook_input = json.load(sys.stdin)
    except (json.JSONDecodeError, OSError, TypeError):
        return 0
    if not isinstance(hook_input, dict):
        return 0

    output = evaluate_gate(hook_input)
    if output is not None:
        json.dump(output, sys.stdout, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 7: Run the runtime test and verify the one-shot invariant**

Run:

```bash
python3 /Users/yuhan/.claude/context-handoff/selftest.py runtime
```

Expected output starts with:

```text
Context handoff self-test: PASS
Gate evaluations: 1001
Automatic emissions: 1
Duplicate emissions: 0
```

It must also print a numeric `Injected payload bytes` value no greater than `1200` and `Banned runtime imports: 0`.

- [ ] **Step 8: Record the Task 1 verification checkpoint**

Run:

```bash
shasum -a 256 \
  /Users/yuhan/.claude/context-handoff/statusline.py \
  /Users/yuhan/.claude/context-handoff/gate.py \
  /Users/yuhan/.claude/context-handoff/selftest.py
```

Expected: three SHA-256 lines. Save them in the implementation report; no Git commit is available for these global files.

---

### Task 2: Add the bounded context-handoff Skill

**Files:**
- Create: `/Users/yuhan/.claude/skills/context-handoff/SKILL.md`
- Test: `/Users/yuhan/.claude/context-handoff/selftest.py`

**Interfaces:**
- Consumes: The active conversation, optional Hook-provided Session ID, targeted Git/task/plan/file evidence.
- Produces: `docs/checkpoints/current-handoff.md`, optional archive checkpoint, and a `.done` marker only when the Session ID is known.

- [ ] **Step 1: Run the Skill test and verify it fails before installation**

Run:

```bash
python3 /Users/yuhan/.claude/context-handoff/selftest.py skill
```

Expected: FAIL with `AssertionError: Missing required file: /Users/yuhan/.claude/skills/context-handoff/SKILL.md`.

- [ ] **Step 2: Create the personal Skill directory**

Run:

```bash
mkdir -p /Users/yuhan/.claude/skills/context-handoff
```

Expected: exit `0` with no output.

- [ ] **Step 3: Write the bounded Skill**

Create `/Users/yuhan/.claude/skills/context-handoff/SKILL.md` with:

````markdown
---
name: context-handoff
description: Create and validate a bounded evidence-based checkpoint when CONTEXT_HANDOFF_REQUIRED appears or before ending a high-context Claude Code session.
when_to_use: Use for context warnings, session handoff, checkpoint preparation, or context overflow prevention.
user-invocable: true
disallowed-tools: Agent Workflow
---

# Context Handoff

Stop feature implementation immediately. The only goal is to create and validate a checkpoint for the next Session.

Do not run `/compact` or `/clear`.
Do not start or resume another task.
Do not invoke Agent or Workflow.
Do not make unrelated production-code changes.
Do not perform a broad repository scan.
Do not claim tests passed unless their results are available.

## Session identity

If the trigger supplied a Session ID, retain it exactly for the completion marker. Do not use `$CLAUDE_SESSION_ID`.

For a manual invocation without a supplied Session ID, create and validate the checkpoint. Do not guess a Session ID or create a `.done` marker.

## Output

Write `docs/checkpoints/current-handoff.md` relative to the active project.

If it already exists, first copy it to:

`docs/checkpoints/archive/<YYYYMMDD-HHMMSS>-handoff.md`

Create missing checkpoint directories when needed.

## Evidence budget

Use the current conversation first.

When Git is available, collect only:

```bash
git branch --show-current
git rev-parse HEAD
git status --short
git diff --stat
git diff --name-only
git log -5 --oneline
```

When Git is unavailable, say so and use targeted filesystem, task, plan, test, and checkpoint evidence.

Read the active task list, active plan, and existing checkpoint when available. Read only files directly relevant to the stopped work.

Do not read a complete transcript. If a critical fact cannot be verified any other way, use at most two bounded transcript excerpts, each no more than 200 lines.

Run at most one focused test command directly required to verify the active task. Do not start a full test suite solely to enrich the handoff. Record unrun tests as `Not run`.

## Checkpoint structure

Write these sections:

# Session Handoff

## 1. Handoff metadata

Timestamp, supplied Session ID or `Not supplied`, project path, branch and HEAD when available, and trigger reason.

## 2. Project goal and architecture

Only architecture relevant to the active work.

## 3. Active task

Task name and ID, goal, status, and exact implementation boundary.

## 4. Complete task status

Classify every known task as Completed, In progress, Open, Blocked, or Unknown. Do not infer unknown states.

## 5. Completed work

Verified outcomes only.

## 6. Current implementation state

Exact file, class or function, test or review phase, and remaining substep.

## 7. Confirmed decisions

For each decision include reason, evidence, and consequence.

## 8. Rejected approaches

Approaches that should not be investigated again and why.

## 9. Changed or relevant files

For each file include path, purpose, modification status, remaining work, and evidence source.

## 10. Tests and validation

Exact command, result, pass/fail counts, known failures, and tests not run.

## 11. Risks and uncertainties

Separate confirmed risks, unverified assumptions, external dependencies, and security or performance risks.

## 12. Repository state

Branch, HEAD, staged, unstaged, untracked files, and whether Git evidence was available.

## 13. Remaining work

Ordered executable steps. The first item identifies the exact file or verification step where the next Session begins.

## 14. Definition of Done

Conditions required to finish the active task.

## 15. Do not reinvestigate

Completed exploration and rejected approaches the next Session must not repeat.

## 16. New-session bootstrap prompt

A copy-paste prompt that tells the next Session to read `CLAUDE.md` when present, read this checkpoint, verify repository or filesystem state, run the recorded baseline test when appropriate, report discrepancies, and continue from the first remaining step.

## Validation

After writing the checkpoint:

1. Read the complete checkpoint once.
2. Verify every referenced path exists or is marked missing.
3. Verify test results are factual.
4. Verify repository state matches current evidence.
5. Replace ambiguous claims with `待確認`.
6. Confirm no production code changed solely for handoff.

## Completion marker

When a Session ID was supplied and validation passed, create:

`~/.claude/context-handoff-state/<supplied-session-id>.done`

Use the sanitized state-key form already present in the trigger path. Do not remove; retain the matching `.triggered` marker for deduplication.

## Final response

Report only:

1. Checkpoint path.
2. Active task and status.
3. Verification or test status.
4. Whether the next Session can safely continue.
5. Instruction to use `/clear` when ready.

Then stop. Do not compact, clear, resume implementation, or start another Session.
````

- [ ] **Step 4: Run the Skill bounds test**

Run:

```bash
python3 /Users/yuhan/.claude/context-handoff/selftest.py skill
```

Expected output starts with `Context handoff self-test: PASS` and reports numeric values for `SKILL.md bytes` no greater than `6000` and `Skill metadata characters` no greater than `500`.

- [ ] **Step 5: Record the Task 2 verification checkpoint**

Run:

```bash
shasum -a 256 /Users/yuhan/.claude/skills/context-handoff/SKILL.md
```

Expected: one SHA-256 line. Save it in the implementation report.

---

### Task 3: Back up and merge the global Claude Code settings

**Files:**
- Create once: `/Users/yuhan/.claude/settings.json.context-handoff-backup-20260720`
- Modify: `/Users/yuhan/.claude/settings.json`
- Test: `/Users/yuhan/.claude/context-handoff/selftest.py`

**Interfaces:**
- Consumes: Existing global Claude Code settings.
- Produces: Startup environment variables, one status-line command, and one command Hook for each of `Stop` and `UserPromptSubmit`.

- [ ] **Step 1: Run the settings test and verify it fails before the merge**

Run:

```bash
python3 /Users/yuhan/.claude/context-handoff/selftest.py settings
```

Expected: FAIL with `AssertionError: Auto compact is not disabled`.

- [ ] **Step 2: Create a non-overwriting rollback backup**

Run:

```bash
backup=/Users/yuhan/.claude/settings.json.context-handoff-backup-20260720
if [ -e "$backup" ]; then
  printf 'Backup already exists: %s\n' "$backup"
else
  cp -p /Users/yuhan/.claude/settings.json "$backup"
  printf 'Created backup: %s\n' "$backup"
fi
```

Expected: exactly one line identifying either the newly created backup or the existing backup. Never overwrite an existing backup.

- [ ] **Step 3: Atomically merge the approved additions without replacing unrelated settings**

Run:

```bash
python3 - <<'PY'
import json
import os
from pathlib import Path

path = Path('/Users/yuhan/.claude/settings.json')
settings = json.loads(path.read_text(encoding='utf-8'))

required_env = {
    'DISABLE_AUTO_COMPACT': '1',
    'CLAUDE_HANDOFF_WARNING_THRESHOLD': '75',
    'CLAUDE_HANDOFF_THRESHOLD': '82',
}
env = settings.setdefault('env', {})
if not isinstance(env, dict):
    raise SystemExit('settings.env exists but is not an object')
for key, required_value in required_env.items():
    existing_value = env.get(key)
    if existing_value not in (None, required_value):
        raise SystemExit(f'Conflicting env value for {key}: {existing_value!r}')
    env[key] = required_value

required_status_line = {
    'type': 'command',
    'command': 'python3 ~/.claude/context-handoff/statusline.py',
}
existing_status_line = settings.get('statusLine')
if existing_status_line not in (None, required_status_line):
    raise SystemExit(f'Conflicting statusLine setting: {existing_status_line!r}')
settings['statusLine'] = required_status_line

hooks = settings.setdefault('hooks', {})
if not isinstance(hooks, dict):
    raise SystemExit('settings.hooks exists but is not an object')
required_group = {
    'hooks': [
        {
            'type': 'command',
            'command': 'python3 ~/.claude/context-handoff/gate.py',
            'timeout': 5,
        }
    ]
}
for event in ('Stop', 'UserPromptSubmit'):
    groups = hooks.setdefault(event, [])
    if not isinstance(groups, list):
        raise SystemExit(f'settings.hooks.{event} exists but is not an array')
    if required_group not in groups:
        groups.append(required_group)

mode = path.stat().st_mode
temporary = path.with_name(f'{path.name}.context-handoff.tmp')
temporary.write_text(
    json.dumps(settings, ensure_ascii=False, indent=2) + '\n',
    encoding='utf-8',
)
os.chmod(temporary, mode)
temporary.replace(path)
print('Claude Code settings merged: PASS')
PY
```

Expected: `Claude Code settings merged: PASS`. Any conflicting pre-existing value stops the merge instead of overwriting it.

- [ ] **Step 4: Verify JSON parsing and exact settings behavior**

Run:

```bash
python3 -m json.tool /Users/yuhan/.claude/settings.json >/dev/null
python3 /Users/yuhan/.claude/context-handoff/selftest.py settings
```

Expected: the JSON parser exits `0`; the self-test prints:

```text
Context handoff self-test: PASS
Settings JSON: PASS
```

- [ ] **Step 5: Compare the backup and merged settings structurally**

Run:

```bash
python3 - <<'PY'
import json
from pathlib import Path

before = json.loads(Path('/Users/yuhan/.claude/settings.json.context-handoff-backup-20260720').read_text())
after = json.loads(Path('/Users/yuhan/.claude/settings.json').read_text())

for key, value in before.items():
    assert after.get(key) == value, f'Existing setting changed: {key}'
assert set(after) - set(before) == {'env', 'statusLine', 'hooks'}
print('Existing settings preserved: PASS')
print('Added top-level keys: env, hooks, statusLine')
PY
```

Expected:

```text
Existing settings preserved: PASS
Added top-level keys: env, hooks, statusLine
```

---

### Task 4: Run final offline verification and produce the installation report

**Files:**
- Verify: `/Users/yuhan/.claude/context-handoff/statusline.py`
- Verify: `/Users/yuhan/.claude/context-handoff/gate.py`
- Verify: `/Users/yuhan/.claude/context-handoff/selftest.py`
- Verify: `/Users/yuhan/.claude/skills/context-handoff/SKILL.md`
- Verify: `/Users/yuhan/.claude/settings.json`

**Interfaces:**
- Consumes: All installed files from Tasks 1–3.
- Produces: Executable scripts, a complete offline verification report, and restart/manual-acceptance instructions.

- [ ] **Step 1: Set script permissions**

Run:

```bash
chmod 755 \
  /Users/yuhan/.claude/context-handoff/statusline.py \
  /Users/yuhan/.claude/context-handoff/gate.py \
  /Users/yuhan/.claude/context-handoff/selftest.py
```

Expected: exit `0` with no output.

- [ ] **Step 2: Compile every Python file**

Run:

```bash
python3 -m py_compile \
  /Users/yuhan/.claude/context-handoff/statusline.py \
  /Users/yuhan/.claude/context-handoff/gate.py \
  /Users/yuhan/.claude/context-handoff/selftest.py
```

Expected: exit `0` with no output.

- [ ] **Step 3: Run the complete offline test suite**

Run:

```bash
python3 /Users/yuhan/.claude/context-handoff/selftest.py all
```

Expected output includes:

```text
Context handoff self-test: PASS
Gate evaluations: 1001
Automatic emissions: 1
Duplicate emissions: 0
Banned runtime imports: 0
Settings JSON: PASS
Python compile: PASS
```

The payload, Skill file, and metadata values must be numeric and within the Global Constraints.

- [ ] **Step 4: Perform a shell-level smoke test in an isolated temporary HOME**

Run:

```bash
temp_home=$(mktemp -d)
trap 'rm -rf "$temp_home"' EXIT

printf '%s\n' '{
  "session_id": "shell-smoke-session",
  "cwd": "/tmp/test-project",
  "workspace": {
    "current_dir": "/tmp/test-project",
    "project_dir": "/tmp/test-project"
  },
  "transcript_path": "/tmp/test-transcript.jsonl",
  "context_window": {
    "used_percentage": 83
  }
}' | HOME="$temp_home" \
    CLAUDE_HANDOFF_WARNING_THRESHOLD=75 \
    CLAUDE_HANDOFF_THRESHOLD=82 \
    python3 /Users/yuhan/.claude/context-handoff/statusline.py

first_output=$(
  printf '%s\n' '{
    "session_id": "shell-smoke-session",
    "cwd": "/tmp/test-project",
    "hook_event_name": "Stop",
    "stop_hook_active": false
  }' | HOME="$temp_home" \
      CLAUDE_HANDOFF_THRESHOLD=82 \
      python3 /Users/yuhan/.claude/context-handoff/gate.py
)

second_output=$(
  printf '%s\n' '{
    "session_id": "shell-smoke-session",
    "cwd": "/tmp/test-project",
    "hook_event_name": "UserPromptSubmit"
  }' | HOME="$temp_home" \
      CLAUDE_HANDOFF_THRESHOLD=82 \
      python3 /Users/yuhan/.claude/context-handoff/gate.py
)

FIRST_OUTPUT="$first_output" SECOND_OUTPUT="$second_output" python3 - <<'PY'
import json
import os

first = os.environ['FIRST_OUTPUT']
second = os.environ['SECOND_OUTPUT']
parsed = json.loads(first)
assert parsed['hookSpecificOutput']['hookEventName'] == 'Stop'
assert 'CONTEXT_HANDOFF_REQUIRED' in parsed['hookSpecificOutput']['additionalContext']
assert second == ''
print('Shell smoke first emission: PASS')
print('Shell smoke duplicate suppression: PASS')
PY
```

Expected status-line output contains `[HANDOFF REQUIRED] Context 83%`. Final output is:

```text
Shell smoke first emission: PASS
Shell smoke duplicate suppression: PASS
```

- [ ] **Step 5: Confirm no model or network process was launched by verification**

Run:

```bash
python3 - <<'PY'
from pathlib import Path

paths = [
    Path('/Users/yuhan/.claude/context-handoff/statusline.py'),
    Path('/Users/yuhan/.claude/context-handoff/gate.py'),
]
for path in paths:
    text = path.read_text()
    for forbidden in ('anthropic', 'openai', 'requests', 'httpx', 'urllib', 'socket'):
        assert forbidden not in text, f'{path.name} contains {forbidden}'
print('Runtime network/API references: 0')
print('Real Claude test sessions launched: 0')
PY
```

Expected:

```text
Runtime network/API references: 0
Real Claude test sessions launched: 0
```

- [ ] **Step 6: Record final hashes and installation facts**

Run:

```bash
shasum -a 256 \
  /Users/yuhan/.claude/context-handoff/statusline.py \
  /Users/yuhan/.claude/context-handoff/gate.py \
  /Users/yuhan/.claude/context-handoff/selftest.py \
  /Users/yuhan/.claude/skills/context-handoff/SKILL.md \
  /Users/yuhan/.claude/settings.json \
  /Users/yuhan/.claude/settings.json.context-handoff-backup-20260720
```

Expected: six SHA-256 lines.

- [ ] **Step 7: Report completion without starting a real Skill Session**

The final implementation response must state:

1. Installed file paths.
2. Backup path.
3. Exact self-test metrics, including payload and Skill sizes.
4. `Automatic emissions: 1` and `Duplicate emissions: 0` across 1,001 evaluations.
5. `Runtime network/API references: 0` and `Real Claude test sessions launched: 0`.
6. That settings changes require restarting Claude Code.
7. That manual `/compact` remains available.
8. That optional end-to-end validation is `/context-handoff` after restart and intentionally consumes model Tokens.
9. Rollback instructions: restore the dated settings backup, remove only the new context-handoff directories after inspection, and restart Claude Code.
10. That no Git commit was created because the target is global configuration and the workspace is not a Git repository.
