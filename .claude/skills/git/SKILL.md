---
name: git
description: Git conventions for palgate-tg-notify — branch naming, commit message style, and the pre-commit check. Use when committing, branching, or preparing a PR in this repository.
---

# Git conventions for palgate-tg-notify

## Branches

- `master` — default branch and PR target.
- `develop` — integration branch.
- Feature work: `feature/<topic>` or `features/<topic>` (both exist historically, e.g. `feature/metrics`, `features/max`). Prefer `feature/<topic>` for new branches.
- Never commit directly to `master`.

## Commit messages

Recent convention is a short snake_case summary of the change, no trailing period:

```
add_bot_handler
rename_chat_handler
set protoc env
```

- One logical change per commit.
- Keep the subject under ~50 characters; no body is customary in this repo.

## Before committing

1. Run `make` (install + proto + mypy). mypy strict is the only quality gate — it must pass.
2. Never stage generated or local files: `models/`, `.env`, `.dev.env`, `palgate.log`, `.mypy_cache/`, `dist/` (all gitignored — do not force-add them).
3. If you changed `protos/*.proto`, regenerate with `make proto` and re-run `make mypy` before committing.
