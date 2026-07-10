# Configuration

All configuration comes from environment variables, parsed by the `Settings` class (pydantic-settings) in [src/config.py](../src/config.py). Variables without a default are required — the service fails fast on startup if one is missing or invalid (`SESSION_TOKEN` must be hex, `URL_USER_LOG` must contain `{device_id}`).

For `make run` / `make docker-dev`, put them in `.dev.env` at the repo root (passed to the container via `--env-file`). Both `.env` and `.dev.env` are gitignored — never commit them.

## Variables

| Variable | Type | Meaning |
| --- | --- | --- |
| `DEVICE_ID` | str | Palgate gate/device ID, substituted into `URL_USER_LOG` |
| `USER_ID` | int | Palgate user ID, used for token generation |
| `SESSION_TOKEN` | str | Palgate session token as a **hex string** (decoded with `bytes.fromhex`) |
| `SESSION_TOKEN_TYPE` | int | `pylgate.types.TokenType` value (e.g. SMS / primary / secondary) |
| `URL_USER_LOG` | str | Log endpoint URL template containing a `{device_id}` placeholder |
| `TZ` | int | UTC offset in hours for log timestamps |
| `TELEGRAM_API_TOKEN` | str | Telegram bot token (used for both notification and log chats) |
| `TELEGRAM_CHAT_ID` | int | Chat that receives gate notifications |
| `TELEGRAM_LOG_CHAT_ID` | int | Chat that receives operational error logs |
| `CRON_DELAY` | int | Polling interval in seconds (≥ 0) |

Optional Max messenger channel (both empty/zero by default — the channel is
enabled only when `MAX_API_TOKEN` is set; the token comes from Max's
@MasterBot):

| Variable | Type | Meaning |
| --- | --- | --- |
| `MAX_API_TOKEN` | str | Max messenger bot token |
| `MAX_CHAT_ID` | int | Max chat that receives gate notifications |

Optional `/release`, `/versions` and `/rollback` support (the commands
reply "not configured" until `GITHUB_TOKEN` is set):

| Variable | Type | Meaning |
| --- | --- | --- |
| `GITHUB_TOKEN` | str | Fine-grained PAT for this repository with **Actions: read and write** (workflow dispatch) and **Contents: read** (releases list). Goes into the runtime env file **on the server**, not into repository secrets |
| `GITHUB_REPO` | str | Repository slug the bot dispatches to (default `m6mok/palgate-tg-notify`) |

Prestable mirror (see [architecture](architecture.md#prestable-mirror)):

| Variable | Default | Meaning |
| --- | --- | --- |
| `SERVICE_ROLE` | `prod` | `prestable` turns the instance into a mirror: the ops bot loop is not started (a second `getUpdates` consumer on the same bot token would 409-conflict the prod instance) and every ops-log-chat record gets a `[prestable]` prefix |

The prestable container runs from its **own env file** (the
`ENV_FILE_PATH_PRESTABLE` deploy secret) and its own volume
(`palgate-prestable-data`). Compared to the prod env file it must set:

- `SERVICE_ROLE=prestable`
- `TELEGRAM_CHAT_ID` — the dedicated prestable chat. The same bot token as
  prod is fine: only `getUpdates` conflicts, `sendMessage` does not
- `TELEGRAM_LOG_CHAT_ID` can stay the prod log chat — the mirror's records
  arrive prefixed with `[prestable]`
- `RESOLVE_ENABLED=false`, **or** a separate `TG_SESSION_STRING`: never
  reuse the prod Telethon session — Telegram may log out a session used
  from two processes at once
- `GITHUB_TOKEN` can be omitted — the ops bot does not run in the mirror

The in-container paths (`STATE_FILE`, `HEARTBEAT_FILE`, …) need no
changes: the separate volume already keeps the mirror's markers, heartbeat
and resolver cache apart from prod.

A minimal prestable env file is the prod one with four lines changed:

```env
# same DEVICE_ID / USER_ID / SESSION_TOKEN / URL_USER_LOG / TZ as prod
SERVICE_ROLE=prestable
TELEGRAM_API_TOKEN=<same bot token as prod>
TELEGRAM_CHAT_ID=-100...        # the prestable chat, NOT the prod one
TELEGRAM_LOG_CHAT_ID=-100...    # prod's log chat is fine ([prestable] prefix)
RESOLVE_ENABLED=false           # or a separate TG_SESSION_STRING
```

Resilience knobs (optional, with defaults):

| Variable | Default | Meaning |
| --- | --- | --- |
| `STATE_FILE` | `data/state.json` | Delivery markers (per source/channel); keep it on a volume so restarts don't lose it |
| `HEARTBEAT_FILE` | `data/heartbeat` | Written by the polling loop each cycle; read by the Docker `HEALTHCHECK` |
| `VERSION_FILE` | `data/version` | Last-seen service version; on startup a change produces an "Updated X → Y" / "Rolled back X → Y" notice in the log chat |
| `LOCK_TIMEOUT` | `60` | Seconds a starting instance waits for the previous one to release the state lock |
| `MAX_BACKOFF` | `300` | Cap (seconds) for exponential backoff between failed poll cycles |
| `ALERT_AFTER_FAILURES` | `10` | Consecutive failed cycles before an alert is sent to the Telegram log chat |

## Example `.dev.env` skeleton

```env
DEVICE_ID=...
USER_ID=...
SESSION_TOKEN=<hex>
SESSION_TOKEN_TYPE=1
URL_USER_LOG=https://.../device/{device_id}/log
TZ=3
TELEGRAM_API_TOKEN=...
TELEGRAM_CHAT_ID=-100...
TELEGRAM_LOG_CHAT_ID=-100...
CRON_DELAY=60
```

A ready-to-copy skeleton lives in [.dev.env.example](../.dev.env.example).

Optional Telegram identity enrichment (off unless `RESOLVE_ENABLED=true`):
resolve a log entry's phone number to a Telegram profile and edit the
notification to append it. The lookup uses a **Telegram user account**
(MTProto `contacts.importContacts`), not the bot — bots cannot resolve a
phone number. See [architecture](architecture.md#identity-enrichment) for the
anti-flood design.

| Variable | Default | Meaning |
| --- | --- | --- |
| `RESOLVE_ENABLED` | `false` | Master switch. When false, nothing below is used and behaviour is unchanged |
| `TG_API_ID` | `0` | Telegram API id from <https://my.telegram.org> |
| `TG_API_HASH` | `""` | Telegram API hash from <https://my.telegram.org> |
| `TG_SESSION` | `data/telethon` | Telethon session name; the file `<name>.session` is created by the one-time login and read by the service. Keep it on the volume |
| `TG_SESSION_STRING` | `""` | A StringSession blob. When set it **takes precedence** over `TG_SESSION` — no session file is used. Best for a headless/release server: the whole session lives in the env file. Treat it like a password |
| `RESOLVER_STATE_FILE` | `data/resolver.json` | Persisted profile cache + FloodWait cooldown; keep on the volume |
| `RESOLVE_MIN_INTERVAL` | `5` | Minimum seconds between lookups (spacing) |
| `RESOLVE_PER_HOUR` | `20` | Rolling hourly cap on lookups |
| `RESOLVE_PER_DAY` | `150` | Rolling daily cap on lookups |
| `RESOLVE_POSITIVE_TTL` | `2592000` | Cache TTL (s) for a found profile (30 days) |
| `RESOLVE_NEGATIVE_TTL` | `259200` | Cache TTL (s) for "no Telegram / privacy closed" (3 days) |
| `RESOLVE_POLL_INTERVAL` | `5` | Background dogon worker tick (s) |

**One-time session login.** The service never logs in interactively; it
needs an already-authorized session. Do the login once (phone → login code →
2FA password) with your `TG_API_ID`/`TG_API_HASH` present. Two forms:

- **Session file** (local runs, or a volume you control):

  ```bash
  make login   # or: TG_API_ID=... TG_API_HASH=... TG_SESSION=data/telethon \
               #        uv run python scripts/telethon_login.py
  ```

  Writes `<TG_SESSION>.session`. In Docker, generate it against the
  `palgate-data` volume (or copy it there) so it survives redeploys.

- **StringSession** (recommended for a release server): print a session blob
  and put it in the server env file as `TG_SESSION_STRING`, next to the other
  secrets — no file to copy onto the volume.

  ```bash
  make login-string   # or: uv run python scripts/telethon_login_string.py
  ```

  Run it once on a machine where you can type the code, then paste the printed
  `TG_SESSION_STRING=...` line into the runtime env file (`ENV_FILE_PATH` on
  the server) and redeploy.

Use the **same** `TG_API_ID`/`TG_API_HASH` for the login and at runtime, and
never run the same session on two machines at once (Telegram may log it out).
If the session is missing or unauthorized at startup, the service logs an
error and runs **without** enrichment rather than crashing.

Anti-flood notes: `importContacts` is rate-limited hard by Telegram. The
resolver caches every number (repeat visitors cost nothing after the first
lookup), spaces calls out, obeys hourly/daily caps, and on a `FloodWait`
enters a persisted cooldown. Each lookup imports the number as a contact of
the resolver account (named after the gate entry) and **leaves it there**
(no cleanup).

## Deploy secrets (GitHub Actions)

The CD workflow ([cd.yml](../.github/workflows/cd.yml)) and the reusable deploy workflow ([deploy.yml](../.github/workflows/deploy.yml)) need these repository secrets:

| Secret | Meaning |
| --- | --- |
| `SSH_HOST` | Deploy server hostname or IP |
| `SSH_USER` | SSH user on the deploy server |
| `SSH_PRIVATE_KEY` | Private key authorized for `SSH_USER@SSH_HOST` |
| `SSH_KNOWN_HOSTS` | Host key line(s) for the server — output of `ssh-keyscan <host>` (used instead of disabling host key checking) |
| `ENV_FILE_PATH` | Absolute path to the runtime env file on the server, passed to `docker run --env-file` |
| `ENV_FILE_PATH_PRESTABLE` | Absolute path to the prestable env file on the server (see the prestable section above); required only for `target: prestable` deploys |
| `PALGATE_SERVER_TOKEN` | (CI only) PAT with read access to the private `m6mok/palgate_server` repo |
| `TELEGRAM_API_TOKEN` | (optional) Bot token for the release announcement CD step |
| `TELEGRAM_LOG_CHAT_ID` | (optional) Chat that receives the release announcement |

The image is published to GHCR as `ghcr.io/m6mok/palgate-tg-notify` using the workflow's own `GITHUB_TOKEN`, tagged with the commit SHA, the semver version from `pyproject.toml`, and `latest`; the server logs in to GHCR with that same ephemeral token during the deploy, so no long-lived registry credentials are stored on the server. After a successful prestable deploy CD creates a git tag and a GitHub Release named after the version (idempotent — redeploys of an existing version skip it) and announces it in the Telegram log chat when the two optional secrets above are set. Prod is updated only by [promote.yml](../.github/workflows/promote.yml) / [rollback.yml](../.github/workflows/rollback.yml).

## Toolchain

- Python is pinned by [.python-version](../.python-version) (3.12); dependencies are locked in `uv.lock`.
- `pylgate` is installed from a pinned git revision (see `[tool.uv.sources]` in [pyproject.toml](../pyproject.toml)).
- `make proto` requires `protoc` installed on the host; the `protobuf-pydantic-gen` plugin is picked up from `.venv/bin` (the Makefile prepends it to `PATH`).
- Runtime writes a rotating `palgate.log` (5 MB × 3 backups) in the working directory, plus `data/state.json` and `data/heartbeat`; in Docker, `data/` is the `palgate-data` volume (`-v palgate-data:/app/data` in `make run` and the CD deploy).
