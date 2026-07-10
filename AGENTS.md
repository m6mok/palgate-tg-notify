 # AGENTS.md

Guidance for AI agents working in this repository. Detailed documentation lives in [docs/](docs/).

## What this project is

A small async Python service that polls the Palgate (smart gate) user access log API, detects new log entries, and pushes notifications to a Telegram chat. There is no web server and no database — one polling loop (`GateWatcher` in [src/service.py](src/service.py)), explicit notifier channels with at-least-once delivery, and per-channel markers persisted in a JSON state file on a Docker volume. Python `logging` is used for operational logs only (stdout, rotating file, Telegram log chat) — not for notification delivery. An ops bot loop (`OpsBot` in [src/bot.py](src/bot.py)) runs alongside the watcher and serves operator commands (`/status`, `/log`, `/poll`, `/pause`, `/resume`, `/release`, `/versions`, `/rollback`, `/prestable`, `/promote`, `/mock`) via `getUpdates` long polling, accepted only from the Telegram log chat. A second **prestable** container (`SERVICE_ROLE=prestable`, own volume and env file) mirrors the polling of a candidate image into a separate chat until it is promoted to prod; the mirror never runs the ops bot (one `getUpdates` consumer per bot token) and prefixes its log-chat records with `[prestable]`. An optional Max messenger channel (`MaxNotifier`) is enabled by setting `MAX_API_TOKEN`. An optional identity enricher (`RESOLVE_ENABLED`) resolves each entry's phone number to a Telegram profile via a Telethon user session and edits the notification to append it, behind a persisted anti-flood cache/rate-limiter (`src/resolver.py`, `src/enrich.py`, `src/telegram_resolver.py`).

- Architecture and data flow: [docs/architecture.md](docs/architecture.md)
- Environment variables and local setup: [docs/configuration.md](docs/configuration.md)

## Commands

Everything goes through the [Makefile](Makefile) and [uv](https://docs.astral.sh/uv/):

| Command | What it does |
| --- | --- |
| `make` (or `make all`) | `install` + `proto` + `lint` + `mypy` + `test` — the full check before committing |
| `make install` | `uv sync` (installs uv itself if missing) |
| `make proto` | Generate `models/log_item_model.py` from `protos/*.proto` (requires `protoc` on PATH; the `--pydantic_out` plugin comes from `.venv`) |
| `make lint` | `uv run ruff check src tests stubs` |
| `make mypy` | `uv run mypy src` — strict mode |
| `make test` | `pytest` over [tests/](tests/) with coverage of `src/`; fails if coverage drops below 90% |
| `make run` (or `make docker-dev`) | Docker build + run with `--env-file .dev.env` |
| `make clean` | Remove `.venv`, generated `models/`, mypy/coverage caches |

The quality gates are **`make lint`, `make mypy` and `make test`** — all three must pass (they also run in CI).

The integration tests in [tests/test_server_integration.py](tests/test_server_integration.py) run the notifier against the mock PalGate server from the private `m6mok/palgate_server` repository. They look for it in `../palgate_server` (override with the `PALGATE_SERVER_DIR` environment variable) and are skipped when it is not checked out.

CI/CD is split across six GitHub Actions workflows:

- [ci.yml](.github/workflows/ci.yml) — runs on PRs to `master` and pushes to `master`: lint, mypy, tests (including the mock-server integration tests — the workflow checks out `m6mok/palgate_server` with the `PALGATE_SERVER_TOKEN` repository secret, a PAT with read access to that repo), Docker build (no deploy).
- [cd.yml](.github/workflows/cd.yml) — triggered by `workflow_run` after **Python CI succeeds on `master`** (a red CI blocks the deploy): builds the image, pushes it to GHCR (`ghcr.io/m6mok/palgate-tg-notify`, tagged with the commit SHA, the semver version from `pyproject.toml`, and `latest`), deploys it to the **prestable mirror** via deploy.yml (prod is untouched), then creates a git tag + GitHub Release named after the version (idempotent) and announces it in the Telegram log chat. A `[skip ci]` marker in the head commit message skips **both** workflows (CI never runs, so CD is never triggered) — but never put it on a PR head commit: `master` is branch-protected and requires the `integration` check, so a skipped CI makes the PR unmergeable.
- [deploy.yml](.github/workflows/deploy.yml) — reusable (`workflow_call`, inputs `image_tag` and `target`: `prod` or `prestable`, picking the container, volume and env file): SSH to the server, pull, swap the container, wait for the healthcheck, revert to the previously running image on failure. Called by cd.yml, promote.yml, rollback.yml and prestable.yml; all runs share the `deploy-master` concurrency group.
- [promote.yml](.github/workflows/promote.yml) — `workflow_dispatch` (input `image_tag`): the normal ship-to-prod path — deploys via deploy.yml with `target: prod`, then stops the prestable container (only after a successful swap). Dispatched from the Actions UI or by the ops bot's `/promote <version>` command.
- [rollback.yml](.github/workflows/rollback.yml) — `workflow_dispatch` (input `image_tag`: release version or commit SHA): redeploys an already-built image to prod via deploy.yml without creating tags/releases, moving `latest` or touching the mirror. Dispatched from the Actions UI or by the ops bot's `/rollback` and `/release <version>` commands (needs `GITHUB_TOKEN` in the server env file, see [docs/configuration.md](docs/configuration.md)).
- [prestable.yml](.github/workflows/prestable.yml) — `workflow_dispatch` (inputs `action`: deploy/stop, `image_tag`): runs any already-built image as the prestable mirror, or stops the mirror without promoting anything. Dispatched from the Actions UI or by the ops bot's `/prestable` command.

## Task workflow

Follow this cycle for every task, no exceptions:

1. **Before starting**: run the full check suite (`make` — install + proto + lint + mypy + test) to confirm a clean baseline. If it fails before you changed anything, report that first — don't mix pre-existing breakage into your task.
2. **Do the work** on a dedicated branch created from up-to-date `origin/master` (base it on another branch only if the user explicitly says so), never directly on `master`.
3. **After finishing**: run the full check suite (`make`) again; it must pass before the task is considered done.
4. **Ship as a PR, don't merge it**: bump `version` in `pyproject.toml` (semver: minor for features, patch for fixes, major for breaking changes) and refresh `uv.lock`, push the branch, and open a PR to `master`. **Never merge the PR yourself** — the user reviews and merges (a merge to `master` deploys to the prestable mirror; prod ships only when the user runs `/promote`).
5. **Git style is uniform** — one branch per task named `feature/<topic>`, short snake_case commit messages, no generated/local files staged. Full conventions: `.claude/skills/git/SKILL.md`.

## Critical constraints

- **`models/` is generated and gitignored.** Never edit `models/log_item_model.py` by hand. Change [protos/log_item.proto](protos/log_item.proto) and run `make proto`. Domain logic on top of the generated models belongs in [src/models.py](src/models.py).
- **mypy is `strict = true`** with the pydantic plugin ([pyproject.toml](pyproject.toml)). Untyped third-party libraries get hand-written stubs in [stubs/](stubs/) (`mypy_path = ["src", "stubs", "models"]`). If you add an untyped dependency, add a stub for it.
- **Flat module layout at runtime.** Modules in `src/` import each other as top-level modules (`from service import …`, `from models import …` → `src/models.py`, `from log_item_model import …` → `models/log_item_model.py`). The Dockerfile flattens `src/*` and `models/*` into `/app`. Tests must use the same flat imports — a `src.`-prefixed import would load a second copy of the module and break `isinstance`/`except` across the boundary. The intended way to run the service is `make run` (Docker); a bare run needs `PYTHONPATH` tweaks and a populated environment.
- **Delivery is at-least-once, keyed by markers.** A channel's marker in the state file advances only after confirmed delivery (see [docs/architecture.md](docs/architecture.md)). Don't "simplify" the order of send → CAS-advance in `src/service.py`: sending after advancing turns an outage into silently lost notifications.
- **The container healthcheck reads a heartbeat deadline** written by the polling loop each cycle (`data/heartbeat`); the CD deploy waits for `healthy` and rolls back otherwise. If you change loop timing, keep the deadline formula in `GateWatcher._touch_heartbeat` generous enough to survive backoff.
- **Secrets live in `.env` / `.dev.env`** (gitignored). Never commit them; see [docs/configuration.md](docs/configuration.md) for the required variables.

## Git conventions

See the project skill in `.claude/skills/git/SKILL.md`. Short version: `master` is the PR target, work happens on `feature/*` / `features/*` branches, and recent commit messages are short snake_case summaries (e.g. `add_bot_handler`).
