# Video workspace verification map

This directory is the maintained source for verifying the user-facing behavior of the local video workspace. Read the index before driving the app, then use the matching feature file as the recipe.

## Baseline preconditions

- Launch with `.cursor/skills/verify-vcc/scripts/control-vcc launch` so the page is served from an empty library.
- In every later command, `control-vcc` means `.cursor/skills/verify-vcc/scripts/control-vcc`, run from the repository root.
- Leave `VCC_VERIFY_DOTENV` unset unless the feature file says Gemini is required.
- Run `control-vcc doctor` and require the printed URL, checkout version, and library directory.
- Never drive an instance that was not started by this verification run.
- A second instance needs its own `VCC_VERIFY_HOME`. It gets another port and another library.

## Driving conventions

- Start every recipe from the baseline state unless its preconditions say otherwise.
- Prefer ARIA roles and accessible names over CSS selectors or DOM position.
- Treat every command as literal. Keep quoted names and flags unchanged.
- Run browser actions through `control-vcc browser`.
- Run the read-only second view through `control-vcc read`.
- Do not remove proof artifacts during cleanup.

## Proof and skip reporting

- Capture the user action and the resulting state, not only the final screen.
- UI proof includes an ARIA snapshot and a full-page screenshot with `影片工作區` visible.
- Mutation proof includes `control-vcc read` of the stored rows.
- Record the feature ID and entry point used with every artifact.
- Report an unreachable path with the attempted command and the unmet precondition.
- Do not report a skipped entry point as verified through a different path.

## Feature entry contract

Each feature file starts with an H1 title and one paragraph describing the user-visible behavior. It then uses exactly four H2 sections in this order.

1. `Sub-features` lists short IDs with one line for each behavior.
2. `How to get to it (user POV)` lists every user entry point.
3. `Driving it with control-vcc` starts with `Preconditions:` and uses labeled bullets that pair each user action with an exact command and observable result.
4. `Gotchas` lists traps that can waste or invalidate a verification run.

Keep implementation details out of the map. Name only user paths, stable handles, required state, commands, and observable proof.

## Features

- [Library](./library.md) covers the empty workspace, the service line, and a search that has nothing to match.
- [Search titles](./search.md) covers title filtering once the library holds saved videos.
- [Load a video](./load-video.md) covers the URL field and the load button.
- [One-click flow](./one-click-flow.md) covers starting, cancelling, and retrying processing.
- [Video Q&A](./video-qa.md) covers asking from a saved subtitle version.
