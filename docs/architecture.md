# Architecture

## Data flow

```text
Palgate API ──HTTP GET──▶ LogUpdater ──diff vs cache──▶ logging "chat" logger
 (user log)               (src/main.py)                  ├─ TelegramHandler → Telegram chat
                                                         ├─ MaxHandler      → Max chat
                                                         └─ stdout + file
```

Every `CRON_DELAY` seconds, `mainloop()` in [src/main.py](../src/main.py) asks `LogUpdater` to fetch the gate's access log, compare it with the last item seen, and emit one message per batch of new entries.

## Logging as the delivery mechanism

The non-obvious design decision: **notifications are sent by logging**. `dictConfig` in `main()` wires three loggers:

| Logger | Handlers | Purpose |
| --- | --- | --- |
| `chat` | `tg_chat` (Telegram), `max_chat` (Max), stdout, file | User-facing notifications — `chat.info(message)` *is* the send |
| `log` | `log` (Telegram log chat), stdout, file | Operational errors (HTTP/validation failures) |
| `default` | stdout, file | Local diagnostics (also used by retry) |

Handlers:

- `telegram_handler.TelegramHandler` — third-party, typed via [stubs/telegram_handler/](../stubs/telegram_handler/).
- `handlers.MaxHandler` ([src/handlers.py](../src/handlers.py)) — home-grown `logging.Handler` that sends via `maxapi.Bot`. **`maxapi` is currently only a stub** (`stubs/maxapi/`), not a real dependency — see the known gap in AGENTS.md.
- `formatter.HtmlFormatter` ([src/formatter.py](../src/formatter.py)) — escapes HTML for Telegram's `parse_mode='HTML'`; the `chat` formatter is message-only (`%(message)s`).

## Polling and deduplication

`LogUpdater` (all in [src/main.py](../src/main.py)):

1. **Auth**: each request generates a fresh `X-Bt-Token` via `pylgate.generate_token(SESSION_TOKEN, USER_ID, SESSION_TOKEN_TYPE)`. `pylgate` is pinned to a git revision in `pyproject.toml`.
2. **Fetch**: `HttpClient` wraps `requests.get` with `retry_call` (3 tries, exponential backoff) on `HTTPError` / `ReadTimeout`.
3. **Validate**: the JSON response is parsed with `ItemResponse.model_validate`; validators in [src/models.py](../src/models.py) reject non-`ok` status, error flags, and empty logs.
4. **Dedup**: the newest previously-seen item is kept in an `aiocache.SimpleMemoryCache` under the key `last_log_item`. New items are collected with `takewhile(item != last_log_item)` from the head of the response. On the very first poll the head item is just stored (no notification) to avoid replaying history.
5. **Notify**: new items are rendered via `Item.__str__` (name, phone link, type/reason emoji) and sent as a single `chat.info()` message; the cache marker is then advanced.
6. **Resilience**: `update_new_items_save()` swallows all exceptions — errors are reported through the `log` logger by `get_items()`, and the loop keeps running.

The cache is in-memory only: a restart re-primes the marker and skips whatever happened while the service was down.

## Model generation pipeline

```text
protos/log_item.proto ──protoc + protobuf-pydantic-gen──▶ models/log_item_model.py (generated, gitignored)
                                                                    ▲
                                        src/models.py wraps it: Item, ItemResponse
```

- `LogItem` / `LogItemResponse` / `LogItemType` are **generated pydantic models** — never edit them; edit the `.proto` and run `make proto`.
- [src/models.py](../src/models.py) adds domain behavior:
  - `Item` — presentation logic: `pn` (phone number normalization to `79…`), `fullname`, emoji signs, `__str__` for the chat message.
  - `ItemResponse` — response validation plus a pre-validator that defaults missing `lastname` fields.
- The Dockerfile copies `models/*` into the image, so `make proto` must run **before** `make run`.

## Type checking setup

mypy runs in `strict` mode with the pydantic plugin. `mypy_path = ["src", "stubs", "models"]` mirrors the flat runtime layout. [stubs/](../stubs/) holds hand-written stubs for untyped dependencies: `aiocache`, `telegram_handler`, `protobuf_pydantic_gen`, `maxapi`. Adding an untyped dependency means adding a stub, or mypy fails.
