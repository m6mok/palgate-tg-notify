FROM python:3.13-slim

WORKDIR /app
COPY . /app

# Installing UV
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN sh -c "\
    apt-get update && \
    apt-get install -y git && \
    uv venv && \
    . .venv/bin/activate"

RUN uv pip install --no-cache -r requirements.txt

ENV PATH="/app/.venv/bin:$PATH"

CMD ["python", "main.py"]
