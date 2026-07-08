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

Resilience knobs (optional, with defaults):

| Variable | Default | Meaning |
| --- | --- | --- |
| `STATE_FILE` | `data/state.json` | Delivery markers (per source/channel); keep it on a volume so restarts don't lose it |
| `HEARTBEAT_FILE` | `data/heartbeat` | Written by the polling loop each cycle; read by the Docker `HEALTHCHECK` |
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

## Deploy secrets (GitHub Actions)

The CD workflow ([cd.yml](../.github/workflows/cd.yml)) needs these repository secrets:

| Secret | Meaning |
| --- | --- |
| `SSH_HOST` | Deploy server hostname or IP |
| `SSH_USER` | SSH user on the deploy server |
| `SSH_PRIVATE_KEY` | Private key authorized for `SSH_USER@SSH_HOST` |
| `SSH_KNOWN_HOSTS` | Host key line(s) for the server — output of `ssh-keyscan <host>` (used instead of disabling host key checking) |
| `ENV_FILE_PATH` | Absolute path to the runtime env file on the server, passed to `docker run --env-file` |
| `PALGATE_SERVER_TOKEN` | (CI only) PAT with read access to the private `m6mok/palgate_server` repo |

The image is published to GHCR as `ghcr.io/m6mok/palgate-tg-notify` using the workflow's own `GITHUB_TOKEN`; the server logs in to GHCR with that same ephemeral token during the deploy, so no long-lived registry credentials are stored on the server.

## Toolchain

- Python is pinned by [.python-version](../.python-version) (3.12); dependencies are locked in `uv.lock`.
- `pylgate` is installed from a pinned git revision (see `[tool.uv.sources]` in [pyproject.toml](../pyproject.toml)).
- `make proto` requires `protoc` installed on the host; the `protobuf-pydantic-gen` plugin is picked up from `.venv/bin` (the Makefile prepends it to `PATH`).
- Runtime writes a rotating `palgate.log` (5 MB × 3 backups) in the working directory, plus `data/state.json` and `data/heartbeat`; in Docker, `data/` is the `palgate-data` volume (`-v palgate-data:/app/data` in `make run` and the CD deploy).
