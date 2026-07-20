# Lessons

## 2026-07-20 — Real subprocess acceptance must cover new output directories

- Classification: missing verification.
- Failure mode: fake extraction tests created parent directories themselves, hiding that real ffmpeg
  could not open a file under a new `--output-dir`.
- Detection signal: the first local excerpt failed before inference with ffmpeg exit 254 and “No
  such file or directory.”
- Prevention rule: the media boundary owns creation of its output parent before launching ffmpeg.
- Tripwire: include a subprocess-fake test that asserts the parent exists at invocation time.

## 2026-07-20 — Canonicalize manifest artifact paths at write time

- Classification: incorrect assumption about repo behavior.
- Failure mode: a relative output directory was embedded in manifest artifact keys, then prepended a
  second time during resume validation.
- Detection signal: full resume looked for `outputs/local-full/outputs/local-full/...` and failed
  before inference.
- Prevention rule: persist absolute artifact paths for new manifests and retain a tested legacy
  fallback for existing cwd-relative manifests.
- Tripwire: run a first command and compatible resume using a relative `--output-dir` in integration
  tests.

## 2026-07-20 — Distinguish empty ASR silence segments from malformed payloads

- Classification: incorrect assumption about repo behavior.
- Failure mode: the mapper treated a long-audio empty Whisper segment as fatal even when many usable
  speech segments were present.
- Detection signal: full inference exited 6 with `MLX Whisper segment text must be nonempty` while
  leaving the manifest safely incomplete.
- Prevention rule: omit empty/whitespace-only silence segments, preserve them in raw output, and
  still reject non-string text or a payload with no usable segments.
- Tripwire: include a valid payload fixture containing an empty segment between speech segments.

## 2026-07-20 — Assert established normalization behavior, not assumed punctuation

- Classification: incorrect assumption about repo behavior.
- Failure mode: a new adapter test expected a terminal full stop that the intentionally
  conservative normalizer does not add for that phrase.
- Detection signal: the focused mapping test returned the correct Traditional Chinese glyph
  conversion without invented punctuation.
- Prevention rule: derive adapter expectations from direct normalization tests or call the
  normalizer in fixture reasoning instead of assuming stylistic output.
- Tripwire: run the smallest existing pure-function example before hard-coding normalization text in
  a new adapter test.

## 2026-07-20 — Confirm local-vs-cloud intent before treating credentials as the terminal blocker

- Classification: misunderstanding requirements.
- Failure mode: the resumed plan treated missing provider credentials as the final blocker without
  first confirming whether the user preferred the already-available local transcription route.
- Detection signal: the user explicitly rejected provider setup and requested local conversion.
- Prevention rule: when credentials are the only blocker and a local equivalent may exist, inspect
  local capabilities and present the local/cloud boundary before asking for secrets or paid opt-in.
- Tripwire: check installed ASR/LLM commands and cached models before declaring provider credentials
  the only viable continuation.

## 2026-07-20 — Filter hardware inventory commands to non-identifying fields

- Classification: security/privacy oversight.
- Failure mode: a broad hardware inventory command returned device identifiers alongside the needed
  chip and memory facts.
- Detection signal: command output included serial/device identity fields unrelated to model fit.
- Prevention rule: query only architecture, chip, core count, and memory, or filter identifying
  fields before output is returned.
- Tripwire: reject hardware-inspection commands containing unfiltered `system_profiler` output.

## 2026-07-20 — Resolve temporary paths before identity assertions

- Classification: incorrect assumption about environment behavior.
- Failure mode: a test compared `/var/...` with the equivalent resolved macOS path `/private/var/...`.
- Detection signal: valid checkpoint validation returned successfully, but the direct `Path` equality assertion failed.
- Prevention rule: when production code intentionally resolves a path, compare its result with `expected.resolve()` in tests.
- Tripwire: include a valid-case assertion using a `TemporaryDirectory` path before exercising negative cases.

## 2026-07-20 — Use narrow cleanup in verification commands

- Classification: unsafe change scope.
- Failure mode: an isolated smoke-test command used `rm -rf` for cleanup and was rejected before the verification batch completed.
- Detection signal: command-policy rejection explicitly identified the broad removal command.
- Prevention rule: prefer a test program's `TemporaryDirectory`; otherwise remove only known-empty temporary directories with `rmdir`.
- Tripwire: scan verification commands for recursive or forced deletion before execution.

## 2026-07-20 — Avoid zsh special parameter names

- Classification: environment-dependent command error.
- Failure mode: a smoke test assigned to zsh's read-only `status` parameter and stopped before cleanup.
- Detection signal: `zsh: read-only variable: status` after the expected negative validation result.
- Prevention rule: use descriptive names such as `validation_status` for captured exit codes.
- Tripwire: run shell smoke tests under the repository's declared shell and check that cleanup executes after expected failures.

## 2026-07-20 — Keep installed Skill validation artifact-free

- Classification: unsafe change scope.
- Failure mode: importing and compiling Skill scripts during self-test left `__pycache__` inside the installed Skill.
- Detection signal: a post-verification file inventory showed generated `.pyc` files.
- Prevention rule: disable bytecode for test-time imports and direct compilation output to a temporary directory.
- Tripwire: inventory the installed Skill after verification and require only declared source/metadata files.

## 2026-07-20 — Treat project checkpoint paths as untrusted input

- Classification: security/privacy oversight.
- Failure mode: fixed-looking project-relative paths could still traverse symlinked files or directories, and archives inherited the process umask instead of a private mode.
- Detection signal: independent probes copied an outside file and wrote an archive outside the project; a `0600` checkpoint became `0644`.
- Prevention rule: reject symlink components, verify confinement under the resolved project root, bound reads before copying, publish atomically, and create archives as `0600`.
- Tripwire: self-test symlinked source/destination paths, oversized inputs, private permissions, and missing-checkpoint bootstrap behavior.

## 2026-07-20 — Align compatibility code with the declared runtime

- Classification: incorrect assumption about environment behavior.
- Failure mode: a Python 3.10 compatibility change conflicted with the repository's Python 3.13 Ruff rules.
- Detection signal: `UP017` required `datetime.UTC` during the post-security-fix lint run.
- Prevention rule: declare the helper's actual minimum Python version and write to that target instead of silently broadening compatibility.
- Tripwire: run Ruff under the repository configuration after any compatibility-oriented edit.

## 2026-07-20 — Assert every expected command outcome

- Classification: missing verification.
- Failure mode: a multi-command smoke test ignored two expected-success exit codes and returned 0 based only on later expected failures.
- Detection signal: captured output showed `Missing checkpoint` for commands labeled as the valid path despite an overall successful shell exit.
- Prevention rule: assert each command's return code and expected output independently in the deterministic self-test.
- Tripwire: treat contradictory command output as a failed verification even when the outer shell exits 0.

## 2026-07-20 — Path checks must stay bound to later I/O

- Classification: security/privacy oversight.
- Failure mode: validating a pathname before opening it left a TOCTOU window where an attacker could replace a parent directory.
- Detection signal: an independent deterministic probe redirected both a checkpoint read and archive publication after the initial path check.
- Prevention rule: traverse fixed relative paths with no-follow directory descriptors and perform open/link/unlink relative to those pinned descriptors.
- Tripwire: replace a parent directory after obtaining its descriptor and prove later reads still use the pinned original directory.

## 2026-07-20 — Re-run formatting checks after test expansion

- Classification: missing verification.
- Failure mode: a newly added security fixture exceeded the repository's 100-character line limit.
- Detection signal: Ruff `E501` after the descriptor-based regression test was added.
- Prevention rule: keep fixture path construction split across readable expressions and rerun Ruff after every test expansion.
- Tripwire: include Ruff in the final parallel verification set, not only before review fixes.
## 2026-07-20 — Avoid zsh's tied `path` parameter in shell loops

- Classification: environment-dependent command error.
- Failure mode: a staged-blob audit loop assigned a filename to zsh's special `path` array, which
  replaced `PATH` and made later `git` invocations unavailable inside the loop.
- Detection signal: the first iteration was followed by repeated `command not found: git` errors;
  staging and repository content were unchanged.
- Prevention rule: use explicit names such as `file_path` for shell loop variables and avoid zsh
  special parameters including `path` and `status`.
- Tripwire: run shell audit helpers under the declared zsh environment and require non-empty,
  numeric output before treating the audit as successful.
