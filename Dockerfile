FROM python:3.12-slim AS lumen-builder

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY lumen/ ./lumen/
RUN uv sync --frozen --no-dev

FROM python:3.12-slim AS lumen-runtime

WORKDIR /app

COPY --from=lumen-builder /app/.venv /app/.venv
COPY pyproject.toml uv.lock LICENSE ./
COPY lumen/ ./lumen/

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && python -m compileall -q lumen \
    && adduser --disabled-password --gecos "" appuser \
    && adduser appuser root \
    && chown -R appuser:appuser /app

ENV PATH="/app/.venv/bin:$PATH"

USER appuser

FROM lumen-runtime AS lumen-api
CMD ["uvicorn", "lumen.main:app", "--host", "0.0.0.0", "--port", "8012"]

FROM lumen-runtime AS lumen-worker
CMD ["python", "-m", "lumen.worker"]
