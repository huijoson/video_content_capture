# Domain Docs

How the engineering skills should consume this repo's domain documentation when exploring the codebase.

This is a **single-context** repo. Its glossary lives in **`CONTEXT.md`** at the repo root (not `GLOSSARY.md`). Wherever a skill says `GLOSSARY.md`, read and write `CONTEXT.md` instead.

## Before exploring, read these

- **`CONTEXT.md`** at the repo root: the project glossary (written in Traditional Chinese, with English terms in parentheses).
- **`docs/adr/`**: read ADRs that touch the area you're about to work in.

If any of these files don't exist, **proceed silently**. Don't flag their absence; don't suggest creating them upfront. The `/domain-modeling` skill (reached via `/grill-with-docs` and `/improve-codebase-architecture`) creates or updates them lazily when terms or decisions actually get resolved.

## File structure

```
/
├── CONTEXT.md          ← glossary
├── docs/adr/
│   ├── 0001-local-browser-interface.md
│   └── ...
└── src/
```

## Use the glossary's vocabulary

When your output names a domain concept (in an issue title, a refactor proposal, a hypothesis, a test name), use the term as defined in `CONTEXT.md`. Don't drift to synonyms the glossary explicitly lists under _Avoid_.

If the concept you need isn't in the glossary yet, that's a signal: either you're inventing language the project doesn't use (reconsider) or there's a real gap (note it for `/domain-modeling`).

## Flag ADR conflicts

If your output contradicts an existing ADR, surface it explicitly rather than silently overriding:

> _Contradicts ADR-0002 (separate playback and QA source), but worth reopening because…_
