FROM ghcr.io/astral-sh/uv:python3.12-alpine

RUN --mount=type=cache,target=/var/cache/apt \
    apk update && apk add git

WORKDIR /app

ENV UV_COMPILE_BYTECODE=1

ENV UV_LINK_MODE=copy

RUN --mount=type=cache,target=/root/.cache/uv \
    --mount=type=bind,source=uv.lock,target=uv.lock \
    --mount=type=bind,source=pyproject.toml,target=pyproject.toml \
    uv sync --frozen --no-install-project --no-dev

ADD pyproject.toml uv.lock ./src/* ./models/* /app/

RUN --mount=type=cache,target=/root/.cache/uv \
    uv sync --frozen --no-dev

ENV PATH="/app/.venv/bin:$PATH"

RUN mkdir -p /app/data \
    && adduser -D app \
    && chown -R app /app
USER app

# Healthy while the polling loop keeps refreshing its heartbeat deadline;
# see src/healthcheck.py and GateWatcher._touch_heartbeat.
HEALTHCHECK --interval=30s --timeout=10s --start-period=60s --retries=3 \
    CMD ["/app/.venv/bin/python", "/app/healthcheck.py"]

CMD ["/app/.venv/bin/python", "/app/main.py"]
