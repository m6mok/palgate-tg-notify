---
name: git
description: Git conventions for palgate-tg-notify — branch naming, commit message style, and the pre-commit check. Use when committing, branching, or preparing a PR in this repository.
---

# Git conventions for palgate-tg-notify

## Branches

- `master` — default branch and PR target.
- Feature work: `feature/<topic>` (one branch per task).
- Never commit directly to `master`.
- **Always base new branches on up-to-date `master`** (`git fetch origin && git switch -c feature/<topic> origin/master`) unless the user explicitly asks to build on another branch.
- **Merged branches are cleaned up automatically**: the repository has "Automatically delete head branches" enabled, so GitHub removes the remote branch when a PR merges. After the user merges, run `git fetch --prune` and delete the local copy (`git branch -d feature/<topic>`) — don't leave stale branches behind.

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

1. Run `make` (install + proto + lint + mypy + test). ruff, mypy strict and the pytest suite (coverage ≥ 90%) are the quality gates — all must pass.
2. Never stage generated or local files: `models/`, `.env`, `.dev.env`, `palgate.log`, `.mypy_cache/`, `dist/` (all gitignored — do not force-add them).
3. If you changed `protos/*.proto`, regenerate with `make proto` and re-run `make mypy` and `make test` before committing.

## Pull requests

- When the task is done (checks green), push the feature branch and open a PR to `master` (`gh pr create`).
- **Never merge a PR yourself** — no `gh pr merge`, no auto-merge. The user reviews and merges; a merge to `master` triggers the production deploy.
- Every PR must carry a version bump: update `version` in `pyproject.toml` (semver: minor for features, patch for fixes/docs-only, major for breaking changes) and refresh `uv.lock` (`uv lock` or `make install`) in the same commit. The running service reports this version at startup and in the ops bot's `/status`.

## CI/CD triggers

- PRs to `master` run CI only (`.github/workflows/ci.yml`: lint, mypy, tests, Docker build).
- Every push to `master` runs CI; CD (`.github/workflows/cd.yml`) is triggered only after CI succeeds (`workflow_run`), deploys to the server via GHCR and creates a git tag + GitHub Release named after the `pyproject.toml` version. A rollback redeploys an older tag via `.github/workflows/rollback.yml` (Actions UI or the ops bot's `/rollback`).
- `master` is protected: no direct pushes — changes land only via PR with the `integration` CI check green and the branch up to date with `master`; force pushes and deletion are blocked (admins included).
- Do **not** use `[skip ci]` on a PR head commit: CI won't run, the required `integration` check never reports, and the PR becomes unmergeable. Amend/push a new commit without the marker to recover.
