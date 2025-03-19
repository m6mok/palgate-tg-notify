FROM python:3.13-slim

WORKDIR /app
COPY . /app

# Installing UV
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN sh -c "\
    uv venv && \
    . .venv/bin/activate && \
    apt-get update && \
    apt-get install -y git && \
    uv pip install -r requirements.txt"

CMD ["python", "main.py"]
