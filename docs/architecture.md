# Architecture

## Data flow

```text
Palgate API ──async HTTP GET──▶ PalgateClient ──ItemResponse──▶ GateWatcher
 (user log)   (src/palgate.py)                  (src/service.py)
                                                      │ diff vs per-channel marker
                                                      ├─▶ TelegramNotifier ─▶ Telegram chat
                                                      │      (src/notify.py)
                                                      ├─▶ MaxNotifier ─▶ Max chat (optional)
                                                      │      (src/notify.py)
                                                      ├─▶ FileStateStore (data/state.json)
                                                      │      (src/state.py)
                                                      └─▶ heartbeat file (data/heartbeat)

Telegram ops chat ──getUpdates──▶ OpsBot ──/status /log /poll /pause /resume
                                (src/bot.py)   │    /release /versions /rollback
                                               ├─▶ GateWatcher (snapshot / poke / pause)
                                               ├─▶ PalgateClient (gate log)
                                               ├─▶ GithubClient (releases, redeploys)
                                               └─▶ TelegramNotifier ─▶ ops chat (replies)
```

Every `CRON_DELAY` seconds `GateWatcher.run()` fetches the gate's access
log, computes the batch of entries each channel has not seen yet, delivers
it, and advances that channel's marker — **only after the channel confirmed
delivery**.

Alongside the polling loop, `OpsBot.run()` long-polls the Telegram Bot API
for operator commands (see [Ops bot](#ops-bot)); both loops share the same
httpx client and stop event and run under one `asyncio.gather`.

## Modules

| Module | Responsibility |
| --- | --- |
| [src/config.py](../src/config.py) | `Settings` (pydantic-settings) with startup validation: hex `SESSION_TOKEN`, `{device_id}` placeholder in the URL, non-negative delays. A broken config crashes immediately. |
| [src/palgate.py](../src/palgate.py) | `PalgateClient` — async httpx client with tenacity retries. Fresh `X-Bt-Token` per attempt (pylgate tokens live a few seconds). Error taxonomy: `TransientFetchError` (network/5xx/429 — retried), `AuthError` (4xx — not retried, carries `status_code`), `InvalidResponseError` (unparsable 2xx). |
| [src/state.py](../src/state.py) | `StateStore` protocol + `MemoryStateStore` / `FileStateStore`. Markers are per **(source, channel)**; `advance()` is compare-and-swap. The file store writes atomically (tmp + rename) and holds an exclusive `flock` leader lock for the process lifetime. A corrupt state file resets to empty markers instead of crashing. |
| [src/notify.py](../src/notify.py) | `Notifier` protocol + `TelegramNotifier` (direct Bot API via httpx, `parse_mode=HTML`) + `MaxNotifier` (Max messenger Bot API, `botapi.max.ru`, token as query param; wired only when `MAX_API_TOKEN` is set). Both retry transport errors, 5xx and 429 (Telegram honours `retry_after`); other 4xx raise a **permanent** `NotifyError`. |
| [src/service.py](../src/service.py) | `GateWatcher` — the polling loop and delivery semantics (below), plus the ops-control surface: `status()` snapshot, `poke()` (immediate cycle), `pause()`/`resume()`. |
| [src/bot.py](../src/bot.py) | `OpsBot` — operator commands from the Telegram ops chat via `getUpdates` long polling (below). |
| [src/github_client.py](../src/github_client.py) | `GithubClient` (+ `ReleaseGateway` protocol) — lists GitHub Releases and dispatches the redeploy workflow for the `/release`, `/versions` and `/rollback` commands; wired only when `GITHUB_TOKEN` is set. |
| [src/healthcheck.py](../src/healthcheck.py) | Container healthcheck: exits non-zero when the heartbeat deadline has passed. |
| [src/main.py](../src/main.py) | Composition root: logging config, leader lock, SIGINT/SIGTERM → graceful stop, httpx client lifecycle, `gather` of the watcher and bot loops. |

## Delivery semantics: at-least-once

The dedup key of a log entry is `"{time}:{sn or userId}"` (`item_key` in
[src/service.py](../src/service.py)) — full-model equality would break as
soon as the API mutates a field of an already-seen entry.

Per channel and per source, the store keeps the key of the newest
**delivered** entry:

1. First poll for a channel: the head key is stored silently (no history
   replay).
2. New entries = `takewhile(key != marker)` from the head of the response;
   the batch is sent oldest-first as one message.
3. On confirmed delivery the marker advances via CAS. On a transient
   delivery failure the marker stays put and the same batch is re-sent next
   cycle — a duplicate is preferred over a lost notification.
4. A **permanent** rejection (e.g. Telegram 400) advances the marker anyway
   and logs the loss, so one poison batch cannot block the channel forever.

Channels are independent: a Telegram outage does not stop another channel
from advancing, and Telegram catches up from its own marker afterwards.

## State file

```json
{
  "version": 1,
  "sources": {
    "<device_id>": {
      "channels": {
        "telegram": { "last_key": "1720434000:79261234567" }
      }
    }
  }
}
```

Lives on a Docker volume (`palgate-data:/app/data`), so restarts and
redeploys do not lose the marker. The `.lock` file next to it carries the
flock: a replacement container started during a deploy waits in
`acquire_lock` (up to `LOCK_TIMEOUT`) until the previous instance exits —
never more than one writer. `advance()` is CAS, so a future multi-instance
setup only needs a shared `StateStore` backend (SQLite/Redis), not a
rewrite of the loop.

## Failure handling in the polling loop

- **Transient errors** (network, 5xx, 429): retried inside `PalgateClient`
  with exponential backoff; if the whole poll still fails, the loop backs
  off exponentially (`cron_delay * 2^n`, capped at `MAX_BACKOFF`, with
  jitter) instead of hammering the API.
- **Persistent failures**: after `ALERT_AFTER_FAILURES` consecutive bad
  cycles an alert goes to the Telegram log chat (and again every N cycles),
  plus a recovery message when polling succeeds again.
- **Unexpected exceptions**: logged with traceback; the loop never dies.
- **Fatal misconfiguration**: caught at startup by `Settings` validation —
  crash fast and let Docker restart policy handle it.

## Ops bot

`OpsBot` ([src/bot.py](../src/bot.py)) long-polls `getUpdates` with the
same bot token the notifiers use and accepts commands **only from the ops
chat** (`TELEGRAM_LOG_CHAT_ID`); messages from any other chat, plain text,
and commands addressed to a different bot (`/cmd@other_bot`) are dropped
silently. Replies go through a `TelegramNotifier` bound to the ops chat,
so delivery retries/backoff are shared with the notification path.

| Command | Effect |
| --- | --- |
| `/status` | Service snapshot (uptime, paused/polling, consecutive failures, last poll/success, next poll ETA, per-channel markers) |
| `/log [n]` | Last `n` gate log entries (default 5, max 20), newest first |
| `/poll` | Immediate poll cycle (`GateWatcher.poke()`), works while paused |
| `/pause` / `/resume` | Suspend/resume polling; the loop keeps writing the heartbeat while paused so the container stays healthy |
| `/release [version]` | Without an argument: release screen — latest release (tag, publish date, title, notes) plus the running version. With one: validates it against the GitHub Releases list and dispatches [rollback.yml](../.github/workflows/rollback.yml) to (re)deploy that release — including redeploying the running version, e.g. to retry a failed deploy. Requires `GITHUB_TOKEN` (see [configuration](configuration.md)) |
| `/versions` | Released versions (up to 10, newest first) with publish dates, the running one marked. Requires `GITHUB_TOKEN` |
| `/rollback [version]` | Without an argument: current version + recent releases. With one: validates it against the GitHub Releases list and dispatches [rollback.yml](../.github/workflows/rollback.yml); refuses the running version. Requires `GITHUB_TOKEN` |
| `/help` | Command reference |

Reliability mirrors the polling loop: the bot loop never dies (transport
errors back off and retry, a broken update is logged and skipped), updates
are acknowledged via the `getUpdates` offset **before** handling so a
poison update cannot wedge the loop, and a pending long poll is abandoned
as soon as the stop event is set, keeping shutdown fast.

## Release & rollback

The CD pipeline ([cd.yml](../.github/workflows/cd.yml)) builds the image
once per merge to `master` and pushes three GHCR tags: the commit SHA, the
semver version from `pyproject.toml`, and `latest`. The deploy itself lives
in a reusable workflow ([deploy.yml](../.github/workflows/deploy.yml),
`workflow_call` with an `image_tag` input): SSH to the server, pull, swap
the container, wait for the healthcheck, revert to the previously running
image on failure. After a successful deploy CD creates a git tag and a
GitHub Release named after the version (idempotent) and announces it in
the Telegram log chat.

[rollback.yml](../.github/workflows/rollback.yml) (`workflow_dispatch`
with an `image_tag` input — a release version or a commit SHA) reuses the
same deploy workflow to redeploy an older image; it never creates tags or
releases and never moves `latest`. It is dispatched from the Actions UI or
by the ops bot's `/rollback` and `/release <version>` commands, and shares
the `deploy-master` concurrency group with CD, so deploys and rollbacks
are serialized.

On startup the service compares its version with the last one recorded in
`VERSION_FILE` (on the data volume) and reports "Updated X → Y" or
"Rolled back X → Y" to the log chat — this doubles as the confirmation
that a deploy or rollback actually swapped the running version.

## Health signal

Each cycle the loop writes a *deadline* timestamp (now + next delay +
margin) into the heartbeat file. The Dockerfile `HEALTHCHECK` runs
[src/healthcheck.py](../src/healthcheck.py), which fails once the deadline
passes — i.e. when the loop itself stopped, not when Palgate is merely
down (an upstream outage keeps the heartbeat fresh while the loop backs
off). The CD pipeline waits for the container to report `healthy` before
considering a deploy successful, and rolls back otherwise.

## Logging

Notifications are **not** sent through logging anymore. `dictConfig` in
[src/main.py](../src/main.py) wires two loggers:

| Logger | Handlers | Purpose |
| --- | --- | --- |
| `log` | Telegram log chat, stdout, rotating file | Lifecycle and operational events: startup (with version), update/rollback notice (version change vs `VERSION_FILE`), shutdown (incl. which signal), service crash with traceback, delivery failures, escalation alerts, recovery notices, first heartbeat failure/restore |
| `default` | stdout, rotating file | Local diagnostics (retries, delivered batches, heartbeat problems) |

The file handler rotates (`palgate.log`, 5 MB × 3 backups), so a long
outage cannot fill the disk.

## Model generation pipeline

```text
protos/log_item.proto ──protoc + protobuf-pydantic-gen──▶ models/log_item_model.py (generated, gitignored)
                                                                    ▲
                                        src/models.py wraps it: Item, ItemResponse
```

- `LogItem` / `LogItemResponse` / `LogItemType` are **generated pydantic
  models** — never edit them; edit the `.proto` and run `make proto`.
- [src/models.py](../src/models.py) adds domain behavior:
  - `Item` — presentation logic: `pn` (phone number normalization to
    `79…`), `fullname`, emoji signs, `__str__` for the chat message.
  - `ItemResponse` — response validation plus a pre-validator that defaults
    missing `lastname` fields.
- The Dockerfile copies `models/*` into the image, so `make proto` must run
  **before** `make run`.

## Type checking setup

mypy runs in `strict` mode with the pydantic plugin over `src/`
(`make mypy` → `uv run mypy src`). `mypy_path = ["src", "stubs", "models"]`
mirrors the flat runtime layout. [stubs/](../stubs/) holds hand-written
stubs for untyped dependencies: `telegram_handler`,
`protobuf_pydantic_gen`. Adding an untyped dependency means adding a stub,
or mypy fails. (httpx and tenacity ship their own type hints.)

## Tests

[tests/](../tests/) is a pytest suite (`make test`); coverage of `src/` is
enforced at 90% minimum. Unit tests cover each module in isolation (httpx
`MockTransport` for the HTTP clients, in-memory store and recording
notifier for the watcher); [tests/test_server_integration.py](../tests/test_server_integration.py)
drives the full stack — real tokens, real HTTP — against the mock PalGate
server from the private `m6mok/palgate_server` repo. Test imports use the
same flat module names as the runtime (`from service import …`) — mixing
them with `src.`-prefixed imports would create duplicate module objects
and break `isinstance`/`except` across the boundary.
