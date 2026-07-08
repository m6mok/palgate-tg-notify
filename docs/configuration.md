# Configuration

All configuration comes from environment variables, parsed by the `Settings` class (pydantic-settings) in [src/main.py](../src/main.py). Every variable is required — the service fails fast on startup if one is missing.

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
| `MAX_API_TOKEN` | str | Max messenger bot token |
| `MAX_CHAT_ID` | int | Max chat that receives gate notifications |
| `CRON_DELAY` | int | Polling interval in seconds |

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
MAX_API_TOKEN=...
MAX_CHAT_ID=...
CRON_DELAY=60
```

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
- Runtime writes a `palgate.log` file in the working directory (the `file` logging handler).
