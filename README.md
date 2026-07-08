# palgate-tg-notify

[![Python CI](https://github.com/m6mok/palgate-tg-notify/actions/workflows/ci.yml/badge.svg)](https://github.com/m6mok/palgate-tg-notify/actions/workflows/ci.yml)

A small async Python service that polls the [Palgate](https://pal-es.com/) (smart gate)
user access log API, detects new log entries, and pushes notifications to a
Telegram chat (and optionally to a [Max](https://max.ru/) chat). No web server,
no database — one polling loop, explicit notifier channels with at-least-once
delivery, and per-channel markers persisted in a JSON state file on a Docker
volume.

## Features

- **Gate notifications** — every new access-log entry (call, phone, admin
  action) lands in your Telegram chat as one formatted message.
- **At-least-once delivery** — a channel's marker advances only after the
  channel confirmed delivery; an outage produces a duplicate at worst, never
  a silently lost notification.
- **Multiple channels** — Telegram out of the box, Max via two env variables;
  channels fail and recover independently.
- **Ops bot** — `/status`, `/log`, `/poll`, `/pause`, `/resume`, `/rollback`
  served from the Telegram log chat via long polling.
- **Self-healing loop** — exponential backoff on upstream failures, escalation
  alerts to the log chat, container healthcheck driven by a heartbeat file.
- **CI/CD** — every merge to `master` is tested, built, pushed to GHCR
  (SHA + semver tags), deployed over SSH with health-check rollback, tagged as
  a GitHub Release, and announced in the log chat. A previous release can be
  redeployed straight from the ops chat with `/rollback <version>`.

## Quickstart

Requirements: Docker, `make`, [uv](https://docs.astral.sh/uv/) (installed
automatically by `make install`), `protoc`.

```sh
git clone https://github.com/m6mok/palgate-tg-notify.git
cd palgate-tg-notify
cp .dev.env.example .dev.env   # or create .dev.env by hand, see docs/configuration.md
make                            # install + proto + lint + mypy + test
make run                        # docker build + run with .dev.env
```

All required environment variables (Palgate credentials, Telegram bot token
and chat IDs, polling interval) are described in
[docs/configuration.md](docs/configuration.md).

## Documentation

- [docs/architecture.md](docs/architecture.md) — data flow, delivery
  semantics, failure handling, ops bot, release & rollback pipeline.
- [docs/configuration.md](docs/configuration.md) — environment variables,
  deploy secrets, toolchain notes.
- [AGENTS.md](AGENTS.md) — repository guide for AI agents and contributors.

## Development

Everything goes through the [Makefile](Makefile): `make lint` (ruff),
`make mypy` (strict), `make test` (pytest, coverage ≥ 90%), `make proto`
(regenerate pydantic models from `protos/*.proto`). Run `make` before every
commit.

## License

[MIT](LICENSE)
