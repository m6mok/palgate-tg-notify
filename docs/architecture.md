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
                                (src/bot.py)   │    /release /versions /rollback /mock
                                               ├─▶ GateWatcher (snapshot / poke / pause)
                                               ├─▶ PalgateClient (gate log)
                                               ├─▶ GithubClient (releases, redeploys)
                                               ├─▶ TelegramNotifier ─▶ prestable chat (/mock)
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
| [src/service.py](../src/service.py) | `GateWatcher` — the polling loop and delivery semantics (below), plus the ops-control surface: `status()` snapshot, `poke()` (immediate cycle), `pause()`/`resume()`. Holds an optional `Enricher`. |
| [src/resolver.py](../src/resolver.py) | Anti-flood layer for phone→profile lookups (below): `ProfileCache` (TTL), `RateLimiter` (spacing + hourly/daily caps + persisted FloodWait cooldown), and `CachingResolver` that composes them over a raw `PhoneResolver`. `FileResolverStore` persists cache + cooldown on the volume. |
| [src/telegram_resolver.py](../src/telegram_resolver.py) | `TelegramContactResolver` — the only MTProto client: a raw `PhoneResolver` doing `contacts.importContacts` via a Telethon **user** session. Translates a Telethon `FloodWaitError` into the layer-neutral `FloodError`. Wired only when `RESOLVE_ENABLED` and the session is authorized. |
| [src/enrich.py](../src/enrich.py) | `Enricher` — renders a batch with cached identities appended (immediate), queues every number for a profile re-check (a rename must be picked up even when cached), and runs a background worker that resolves them at the limiter's pace and edits the messages (dogon). All best-effort; never affects delivery. |
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
| `/prestable [version\|stop]` | Without an argument: releases + usage. With a version: validates it and dispatches [prestable.yml](../.github/workflows/prestable.yml) to run that image as the prestable mirror. `stop` removes the mirror container without touching prod. Requires `GITHUB_TOKEN` |
| `/promote <version>` | Validates the version and dispatches [promote.yml](../.github/workflows/promote.yml): deploy to prod first, stop the prestable mirror after a successful swap. Requires `GITHUB_TOKEN` |
| `/mock <firstname> <lastname> <phone>` | Posts a fabricated gate entry to the **prestable** chat — never to the prod one — through the watcher's real delivery path (`GateWatcher.send_batch`): the enricher renders cached identities in and queues the number for background resolution, exactly like a polled entry; only the markers stay untouched. An unknown number spends real anti-flood budget. Requires `PRESTABLE_TELEGRAM_CHAT_ID` in the prod env file |
| `/resolve [reset]` | Without an argument: resolver cache state (cached numbers, active flood cooldown). `reset` drops every cached identity so the next entries are looked up afresh; the anti-flood limiter state (cooldown, hourly/daily budget) deliberately survives the reset. Requires the identity enricher to be running (`RESOLVE_ENABLED`) |
| `/help` | Command reference |

Reliability mirrors the polling loop: the bot loop never dies (transport
errors back off and retry, a broken update is logged and skipped), updates
are acknowledged via the `getUpdates` offset **before** handling so a
poison update cannot wedge the loop, and a pending long poll is abandoned
as soon as the stop event is set, keeping shutdown fast.

## Release & rollback

The CD pipeline ([cd.yml](../.github/workflows/cd.yml)) builds the image
once per merge to `master` and pushes three GHCR tags: the commit SHA, the
semver version from `pyproject.toml`, and `latest` — then deploys it to
the **prestable mirror**, not to prod (see
[Prestable mirror](#prestable-mirror)). The deploy itself lives in a
reusable workflow ([deploy.yml](../.github/workflows/deploy.yml),
`workflow_call` with `image_tag` and `target` inputs — `prod` or
`prestable` picks the container, volume and env file): SSH to the server,
pull, swap the container, wait for the healthcheck, revert to the
previously running image on failure. After a successful prestable deploy
CD creates a git tag and a GitHub Release named after the version
(idempotent) and announces it in the Telegram log chat.

Prod changes only through two dispatch workflows, both reusing
deploy.yml with `target: prod`:

- [promote.yml](../.github/workflows/promote.yml) (`workflow_dispatch`,
  input `image_tag`) — the normal ship path: deploy to prod, then stop the
  prestable mirror (only after a successful swap, so a failed promote
  leaves the candidate under observation). Dispatched by `/promote`.
- [rollback.yml](../.github/workflows/rollback.yml) (`workflow_dispatch`,
  input `image_tag` — a release version or a commit SHA) — redeploys an
  older image; it never creates tags or releases, never moves `latest`
  and does not touch the mirror. Dispatched from the Actions UI or by the
  ops bot's `/rollback` and `/release <version>` commands.

All deploy workflows share the `deploy-master` concurrency group, so
prod and prestable swaps are serialized on the server.

On startup the service compares its version with the last one recorded in
`VERSION_FILE` (on the data volume) and reports "Updated X → Y" or
"Rolled back X → Y" to the log chat — this doubles as the confirmation
that a deploy or rollback actually swapped the running version.

## Prestable mirror

A candidate build proves itself on real traffic before it is trusted with
the prod chat. The mirror is a second container
(`palgate-tg-notify-prestable`) on the same server, with its own volume
(`palgate-prestable-data`) and its own env file
([configuration](configuration.md)): `SERVICE_ROLE=prestable`, the
dedicated prestable chat in `TELEGRAM_CHAT_ID`, the same bot token as
prod. It polls the same gate with the full delivery semantics — markers,
heartbeat, at-least-once — but its notifications land in the prestable
chat, so a bad change is caught there and never leaks to prod.

`SERVICE_ROLE=prestable` changes exactly two things in the process:

- The ops bot loop is **not started** — `getUpdates` allows a single
  consumer per bot token and the prod instance owns that stream
  (`sendMessage` from two processes is fine). All operator commands,
  including the ones that manage the mirror, are served by prod.
- Records sent to the shared ops log chat are prefixed with
  `[prestable]`, so the two instances stay tellable apart.

The Telethon resolver must never share prod's session (Telegram may log
out a session used from two machines at once): the mirror's env file
either keeps `RESOLVE_ENABLED` off or carries its own
`TG_SESSION_STRING`.

Lifecycle: every merge to `master` lands on the mirror (cd.yml). The
operator watches the prestable chat and ships with `/promote <version>` —
prod deploys first, the mirror is stopped only after a successful swap,
and from that moment nothing more reaches the prestable chat.
`/prestable <version>` puts any released version back on the mirror;
`/prestable stop` kills a bad candidate without promoting anything.

## Health signal

Each cycle the loop writes a *deadline* timestamp (now + next delay +
margin) into the heartbeat file. The Dockerfile `HEALTHCHECK` runs
[src/healthcheck.py](../src/healthcheck.py), which fails once the deadline
passes — i.e. when the loop itself stopped, not when Palgate is merely
down (an upstream outage keeps the heartbeat fresh while the loop backs
off). The CD pipeline waits for the container to report `healthy` before
considering a deploy successful, and rolls back otherwise.

## Identity enrichment

Optional (`RESOLVE_ENABLED`). A delivered notification lists gate entries by
phone number; the enricher looks each number up in Telegram and edits the
message to append the matching identity — the automated equivalent of the
mobile app's "dive into a number". Resolution needs a **user account**
(MTProto `contacts.importContacts`); the notification bot cannot do it.

```text
GateWatcher._deliver ──render(batch)──▶ Telegram (send, returns message_id)
        │                                   ▲
        └──track(message_id, batch)──▶ Enricher ──dogon queue──▶ background worker
                                            │                          │
                                            ▼                          ▼
                                     CachingResolver ◀───resolve()─────┘
                                       │  cache → limiter → raw
                                       ▼
                              TelegramContactResolver (Telethon user session)
```

Two paths, both best-effort — a failure never touches delivery or the marker:

1. **Immediate** — `render` folds in whatever is already in the resolver
   cache when the message is first built, so warm numbers arrive enriched
   with no edit.
2. **Background dogon** — `track` queues the numbers that still need a
   network lookup; the worker resolves them at the limiter's pace and
   re-edits the message as identities arrive. The batch is edited **whole**
   (one message per poll batch), matching the existing delivery shape.

**Anti-flood** is the point of `CachingResolver`, since `importContacts` is
rate-limited hard. Each lookup passes three guards, cheapest first:

- **TTL cache** — the same people use the gate daily, so a warm cache means
  almost no calls. A found profile is cached long (`RESOLVE_POSITIVE_TTL`,
  default 30 days); a definitive miss short (`RESOLVE_NEGATIVE_TTL`, default
  3 days) so someone joining Telegram later is picked up.
- **Token-bucket limiter** — minimum spacing plus rolling hourly and daily
  caps, all configurable and conservative by default.
- **FloodWait cooldown** — a `FloodError` disables lookups for the window
  Telegram asked for (plus a margin). The cache and the cooldown deadline are
  persisted (`FileResolverStore` → `data/resolver.json`), so a restart honours
  an open cooldown instead of walking straight back into the flood.

Numbers blocked by a guard return `DEFERRED` and stay in the dogon queue for
a later round. The queue itself is **in-memory**: a restart drops pending
dogon (those messages keep their last edited state), but the persisted cache
means future messages still benefit. A batch leaves the queue once every
number is known, once an edit is permanently rejected, or once it outlives
`batch_ttl`. Telegram reports a saved contact under *our* contact-list name,
not the person's own profile name — so each lookup imports the number, deletes
the contact (the delete response carries the self-set profile name), and
re-saves the contact under that actual name. Every delivered batch queues its
numbers for a background re-check — even ones already cached — so each
appearance at the gate refreshes the name in the message and in the contact
book (numbers cached as absent wait out `RESOLVE_NEGATIVE_TTL` instead). The
appended identity shows the **name the user set
on their own Telegram profile**, linked to `t.me/<username>` — or an in-app
`tg://user?id` link, and the `@username` as the label, when there is no
username.

## Logging

Notifications are **not** sent through logging anymore. `dictConfig` in
[src/main.py](../src/main.py) wires two loggers:

| Logger | Handlers | Purpose |
| --- | --- | --- |
| `log` | Telegram log chat (via aiologging), stdout, rotating file | Lifecycle and operational events: startup (with version), update/rollback notice (version change vs `VERSION_FILE`), shutdown (incl. which signal), service crash with traceback, delivery failures, escalation alerts, recovery notices, first heartbeat failure/restore |
| `default` | stdout, rotating file | Local diagnostics (retries, delivered batches, heartbeat problems) |

The file handler rotates (`palgate.log`, 5 MB × 3 backups), so a long
outage cannot fill the disk.

Delivery to the Telegram log chat is asynchronous:
[aiologging](https://github.com/m6mok/aiologging)'s `StdlibBridgeHandler`
(attached to the stdlib `log` logger) forwards records into aiologging's
queue, where an `AsyncTelegramHandler` (HTML-escaping formatter, httpx
backend, batching under the 4096-char limit, 429-aware retries) posts them
from a background worker — a slow or down Telegram API never blocks the
event loop, the poll cycle, or the heartbeat. Log calls stay plain stdlib
`logging`; on shutdown `main()` drains the queue with
`aiologging.shutdown(timeout=...)`, and an atexit hook gives undelivered
records a final ~2s best-effort flush.

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
stubs for untyped dependencies: `protobuf_pydantic_gen` and the subset of
`telethon` the resolver uses. Adding an untyped dependency means adding a
stub, or mypy fails. (httpx, tenacity and aiologging ship their own type
hints.)

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
