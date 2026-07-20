# Context Handoff Design

- **Date:** 2026-07-20
- **Status:** Approved design
- **Scope:** Personal Claude Code configuration under `~/.claude`
- **Target environment:** Claude Code 2.1.215, Python 3.13.1, macOS

## 1. Goal

Create a personal `context-handoff` Skill and supporting local automation that prepares an evidence-based checkpoint before the active context window is exhausted.

The system must:

1. Warn at 75% context usage.
2. Automatically request a handoff at 82% context usage.
3. Add at most one automatic model continuation per Claude Session.
4. Avoid broad repository exploration, full transcript reads, subagents, workflows, and unnecessary test suites during handoff.
5. Never run `/clear` or `/compact` automatically.
6. Produce `docs/checkpoints/current-handoff.md` so a new Session can continue safely.
7. Preserve all existing Claude Code settings while adding the new behavior.
8. Include local, deterministic tests that demonstrate the automatic trigger cannot repeatedly amplify Token usage.

## 2. Non-goals

This design does not:

- Guarantee that a handoff uses zero model Tokens. Creating a useful checkpoint necessarily requires model work.
- Automatically start a new Claude Session.
- Automatically clear or compact the current Session.
- Guarantee recovery from API outages. If the one automatic attempt fails, the user retries manually.
- Scan or summarize the complete transcript by default.
- Replace Git, task tracking, plans, or existing project documentation.
- Introduce a remote service, daemon, database, or network dependency.

## 3. Existing environment

The existing global settings file is `~/.claude/settings.json`. It currently contains model, plugin, effort, theme, and interface settings, but no `env`, `statusLine`, or `hooks` sections.

No personal `~/.claude/skills/context-handoff` Skill or `~/.claude/context-handoff` scripts currently exist.

The current working directory is not a Git repository. This does not affect the personal installation, but the design specification itself cannot be committed from this directory.

## 4. Architecture

```text
~/.claude/
├── settings.json
├── context-handoff/
│   ├── statusline.py
│   ├── gate.py
│   └── selftest.py
├── context-handoff-state/
│   └── <session-id>.{json,triggered,done}
└── skills/
    └── context-handoff/
        └── SKILL.md
```

Each project receives handoff output at:

```text
docs/checkpoints/
├── current-handoff.md
└── archive/
    └── <YYYYMMDD-HHMMSS>-handoff.md
```

### 4.1 `statusline.py`

Responsibilities:

- Read Claude Code status-line JSON from stdin.
- Parse `session_id`, `workspace.project_dir`, `workspace.current_dir`, `cwd`, `transcript_path`, and `context_window.used_percentage`.
- Treat a missing or null percentage as zero.
- Atomically write a small per-Session state JSON file.
- Print a green, yellow, red, pending, or ready status.

It must not:

- Read Git state.
- Read a transcript.
- Run subprocesses.
- Import networking or model-provider packages.
- Call an API.

Status states:

| Condition | Display |
| --- | --- |
| `< 75%` | `[CONTEXT] N%` |
| `75–81%` | `[CONTEXT WARNING] N%` |
| `>= 82%`, not triggered | `[HANDOFF REQUIRED] N%` |
| `>= 82%`, triggered but not done | `[HANDOFF PENDING] N% | run /context-handoff if needed` |
| `.done` exists | `[HANDOFF READY] N% | /clear when ready` |

The official Claude Code status-line documentation states that status-line commands run locally and do not consume API Tokens.

### 4.2 `gate.py`

Responsibilities:

- Handle only `Stop` and `UserPromptSubmit` Hook events.
- Read the state written by `statusline.py`.
- Emit a compact `hookSpecificOutput.additionalContext` only for the first eligible event in a Session.
- Include the actual Hook input `session_id` in the injected message.

Decision order:

1. Invalid input or unsupported event: exit with no output.
2. Missing state file: exit with no output.
3. Existing `.done`: exit with no output.
4. Existing `.triggered`: exit with no output.
5. `Stop` with `stop_hook_active: true`: exit with no output.
6. Context below the configured threshold: exit with no output.
7. Atomically create `.triggered`.
8. Emit one valid JSON object containing `additionalContext`.

There is no time-based cooldown and no automatic retry. This creates a hard upper bound of one automatic handoff injection per Session.

The injected message must be no more than 1,200 UTF-8 bytes and contain only:

- A stable event marker.
- Current context percentage.
- Actual Session ID.
- The requirement to invoke `context-handoff`.
- The relative checkpoint path.
- Instructions not to compact, clear, resume feature work, or start another task.
- The requirement to stop after validating the checkpoint.

The message does not include the transcript, Git output, a full project path, or other large evidence.

### 4.3 `SKILL.md`

The Skill remains directly available as `/context-handoff` and may also be invoked by Claude after the Hook injects the trigger.

Frontmatter requirements:

- `name: context-handoff`
- A concise `description` with the key trigger first.
- A concise `when_to_use`.
- `user-invocable: true`
- `disallowed-tools: Agent Workflow`

The combined always-listed metadata must remain below 500 characters. The complete `SKILL.md` file must remain below 6,000 UTF-8 bytes; its full instructions are loaded only when invoked.

The Skill must immediately stop feature implementation and must not:

- Run `/compact` or `/clear`.
- Start another task.
- Invoke Agent or Workflow.
- Perform broad repository exploration.
- Read a complete large transcript.
- Make unrelated production-code changes.
- Report assumed test success.

Evidence budget:

- Prefer the current conversation.
- Collect only targeted Git, task, plan, checkpoint, and file evidence.
- Do not read the transcript unless a critical fact cannot otherwise be verified.
- If transcript evidence is necessary, read no more than two bounded excerpts.
- Run at most one focused, directly relevant test command.
- Do not start a full test suite solely to enrich the checkpoint.
- Stop immediately after checkpoint validation and Session-state marking.

Checkpoint structure:

1. Handoff metadata.
2. Relevant project goal and architecture.
3. Active task and exact implementation boundary.
4. Complete known task status.
5. Verified completed work.
6. Current implementation state.
7. Confirmed decisions with evidence and consequences.
8. Rejected approaches.
9. Changed or relevant files.
10. Tests and validation.
11. Risks and uncertainties.
12. Repository state.
13. Ordered remaining work.
14. Definition of Done.
15. Do-not-reinvestigate list.
16. New-Session bootstrap prompt.

The Skill must archive an existing `current-handoff.md` before replacing it.

For an automatic invocation, the Hook-provided Session ID is used to create `~/.claude/context-handoff-state/<session-id>.done` after validation. The matching `.triggered` marker is retained as an audit and deduplication marker. The implementation must not depend on `$CLAUDE_SESSION_ID`, because that variable is not documented as a guaranteed Claude Code command environment variable.

For a manual invocation without a Hook-provided Session ID, the Skill creates and validates the checkpoint but does not guess which Session marker to update.

### 4.4 `selftest.py`

The self-test uses a temporary HOME and mock JSON. It does not start Claude Code or call an API.

It validates:

1. 74% produces the normal state.
2. 75% produces the warning state.
3. 82% produces the required state.
4. 81% Gate evaluation emits nothing.
5. The first eligible 82% event emits valid JSON.
6. One initial evaluation plus 1,000 duplicate evaluations produces exactly one total emission.
7. `stop_hook_active: true` emits nothing.
8. A `.done` marker emits nothing.
9. Invalid JSON produces no traceback.
10. Injected context is at most 1,200 UTF-8 bytes.
11. Skill metadata is at most 500 characters.
12. The complete `SKILL.md` file is at most 6,000 UTF-8 bytes.
13. The Skill contains Agent and Workflow restrictions.
14. Runtime scripts contain no network or provider imports.
15. All Python files compile.
16. The merged settings file parses as JSON.

The report includes actual evaluation count, emission count, duplicate count, payload size, compile status, and JSON status.

## 5. Data flow

```text
Claude Code status event
  -> statusline.py
  -> local per-Session state JSON

Context below 82%
  -> gate.py returns no output
  -> no additional model continuation

Context at or above 82%
  -> first eligible Stop or UserPromptSubmit
  -> gate.py creates .triggered
  -> gate.py injects one compact additionalContext
  -> Claude invokes context-handoff
  -> checkpoint is created and validated
  -> .done is created
  -> Claude stops and waits for the user
```

`UserPromptSubmit` is only a backup for cases where no eligible Stop occurred, such as an API-error ending. It cannot inject again after `.triggered` exists.

## 6. Error handling

### Status-line errors

Malformed input, missing fields, or file-write errors must not crash Claude Code. The script exits cleanly and prints only information it can verify.

### Gate state errors

If state cannot be read or `.triggered` cannot be created, the Gate emits nothing. This is a Token-safe fail-closed policy. The red status line remains the manual recovery path.

### Handoff failure

If the automatic handoff continuation fails, the `.triggered` marker remains. No automatic retry occurs. The status line displays `HANDOFF PENDING`, and the user can invoke `/context-handoff` manually.

### Completed handoff

Once `.done` exists, neither Hook emits further context. The status line tells the user that `/clear` may be run after reviewing the checkpoint.

## 7. Token-safety model

The design separates local work from model work:

| Operation | API Token effect |
| --- | --- |
| Status-line execution | None; documented local execution |
| State-file reads/writes | None |
| Below-threshold Hook execution | None; no context output |
| Duplicate Hook execution | None; no context output |
| First eligible Hook output | One automatic continuation entry point |
| Skill listing metadata | Small bounded context entry |
| Invoked Skill file | Loaded once, complete file bounded to 6,000 bytes |
| Handoff evidence and writing | Uses model Tokens, bounded by workflow restrictions |

The system does not claim an exact Token count from byte size because tokenization depends on model and content. Instead, it enforces the stronger operational controls:

- At most one automatic continuation entry point per Session.
- A hard injected-payload byte limit.
- No subagents or workflows.
- No broad exploration.
- No automatic retry.
- No extra Claude Session created for testing.

## 8. Settings merge

The existing settings must be preserved. The implementation adds only:

```json
{
  "env": {
    "DISABLE_AUTO_COMPACT": "1",
    "CLAUDE_HANDOFF_WARNING_THRESHOLD": "75",
    "CLAUDE_HANDOFF_THRESHOLD": "82"
  },
  "statusLine": {
    "type": "command",
    "command": "python3 ~/.claude/context-handoff/statusline.py"
  },
  "hooks": {
    "Stop": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/context-handoff/gate.py",
            "timeout": 5
          }
        ]
      }
    ],
    "UserPromptSubmit": [
      {
        "hooks": [
          {
            "type": "command",
            "command": "python3 ~/.claude/context-handoff/gate.py",
            "timeout": 5
          }
        ]
      }
    ]
  }
}
```

`DISABLE_AUTO_COMPACT=1` disables automatic compaction while leaving manual `/compact` available. `DISABLE_COMPACT` must not be set.

A Claude Code restart is required after installation so startup-read environment settings and the new Skill are loaded.

## 9. Acceptance criteria

Implementation is accepted when:

- Existing global settings remain unchanged except for the approved additions.
- All three scripts and the Skill file exist in the approved locations.
- Files have appropriate executable permissions where required.
- `python3 -m py_compile` passes.
- `python3 ~/.claude/context-handoff/selftest.py` passes.
- The self-test reports 1 emission across 1,001 eligible/duplicate evaluations.
- Duplicate emissions equal zero.
- The injected payload is no more than 1,200 bytes.
- No runtime script contains an API or network dependency.
- The settings file parses as JSON.
- Manual `/compact` remains available by configuration.
- No real Claude test Session is started automatically.
- The user is told to restart Claude Code and may then manually run `/context-handoff` for optional end-to-end acceptance.

## 10. Rollback

Rollback consists of:

1. Removing the added `env`, `statusLine`, `Stop`, and `UserPromptSubmit` entries from `~/.claude/settings.json` while retaining unrelated settings.
2. Removing `~/.claude/context-handoff` and `~/.claude/skills/context-handoff` if no longer needed.
3. Optionally removing `~/.claude/context-handoff-state`.
4. Restarting Claude Code.

Project checkpoint files are retained unless the user explicitly chooses to remove them.

## 11. Official references

- Status line: <https://code.claude.com/docs/en/statusline>
- Hooks: <https://code.claude.com/docs/en/hooks>
- Skills: <https://code.claude.com/docs/en/skills>
- Environment variables: <https://code.claude.com/docs/en/env-vars>
