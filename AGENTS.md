# AGENTS.md

Guidance for AI agents working in this repository. Detailed documentation lives in [docs/](docs/).

## What this project is

A small async Python service that polls the Palgate (smart gate) user access log API, detects new log entries, and pushes notifications to a Telegram chat and a Max messenger chat. There is no web server and no database — one polling loop, an in-memory cache, and Python `logging` as the delivery mechanism.

- Architecture and data flow: [docs/architecture.md](docs/architecture.md)
- Environment variables and local setup: [docs/configuration.md](docs/configuration.md)

## Commands

Everything goes through the [Makefile](Makefile) and [uv](https://docs.astral.sh/uv/):

| Command | What it does |
| --- | --- |
| `make` (or `make all`) | `install` + `proto` + `mypy` — the full check before committing |
| `make install` | `uv sync` (installs uv itself if missing) |
| `make proto` | Generate `models/log_item_model.py` from `protos/*.proto` (requires `protoc` on PATH; the `--pydantic_out` plugin comes from `.venv`) |
| `make mypy` | `uv run mypy .` — strict mode, this is the only quality gate |
| `make run` (or `make docker-dev`) | Docker build + run with `--env-file .dev.env` |
| `make clean` | Remove `.venv`, generated `models/`, mypy cache |

There are no tests and no linter — **`make mypy` must pass**; it is the de facto CI check.

## Task workflow

Follow this cycle for every task, no exceptions:

1. **Before starting**: run the full check suite (`make`) to confirm a clean baseline. If it fails before you changed anything, report that first — don't mix pre-existing breakage into your task. (There is no test suite yet; `make` — install + proto + mypy — plays that role. If tests are ever added, they join this step.)
2. **Do the work** on a dedicated branch, never directly on `master`.
3. **After finishing**: run the full check suite (`make`) again; it must pass before the task is considered done.
4. **Git style is uniform** — one branch per task named `feature/<topic>`, short snake_case commit messages, no generated/local files staged. Full conventions: `.claude/skills/git/SKILL.md`.

## Critical constraints

- **`models/` is generated and gitignored.** Never edit `models/log_item_model.py` by hand. Change [protos/log_item.proto](protos/log_item.proto) and run `make proto`. Domain logic on top of the generated models belongs in [src/models.py](src/models.py).
- **mypy is `strict = true`** with the pydantic plugin ([pyproject.toml](pyproject.toml)). Untyped third-party libraries get hand-written stubs in [stubs/](stubs/) (`mypy_path = ["src", "stubs", "models"]`). If you add an untyped dependency, add a stub for it.
- **Flat module layout at runtime.** `src/main.py` imports `models` (→ `src/models.py`) and `log_item_model` (→ `models/log_item_model.py`) as top-level modules. The Dockerfile flattens `src/*` and `models/*` into `/app`. The intended way to run the service is `make run` (Docker); a bare `uv run src/main.py` needs `PYTHONPATH` tweaks and a populated environment.
- **Known gap on branch `features/max`:** `src/handlers.py` imports `maxapi`, but `maxapi` exists only as a type stub in `stubs/maxapi/` and is **not** declared in `pyproject.toml` — the service will fail at import time until the real dependency is added.
- **Secrets live in `.env` / `.dev.env`** (gitignored). Never commit them; see [docs/configuration.md](docs/configuration.md) for the required variables.

## Git conventions

See the project skill in `.claude/skills/git/SKILL.md`. Short version: `master` is the PR target, work happens on `feature/*` / `features/*` branches, and recent commit messages are short snake_case summaries (e.g. `add_bot_handler`).
