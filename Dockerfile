FROM python:3.13-slim

WORKDIR /app
COPY . /app

# Installing UV
RUN curl -LsSf https://astral.sh/uv/install.sh | sh
RUN uv pip install -r requirements.txt

CMD ["python", "main.py"]
