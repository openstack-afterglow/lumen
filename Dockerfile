ARG AFTERGLOW_CRYPTO_REF=""

FROM python:3.12-slim AS lumen-builder
ARG AFTERGLOW_CRYPTO_REF=""

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY pyproject.toml uv.lock LICENSE ./
RUN uv sync --frozen --no-dev --no-install-project

COPY lumen/ ./lumen/
COPY lumen_console/ ./lumen_console/
RUN uv sync --frozen --no-dev
RUN if [ -n "$AFTERGLOW_CRYPTO_REF" ]; then \
    uv pip install --python /app/.venv/bin/python --reinstall-package afterglow-crypto --no-deps "afterglow-crypto @ git+https://github.com/openstack-afterglow/afterglow-crypto.git@$AFTERGLOW_CRYPTO_REF"; \
    fi

FROM lumen-builder AS lumen-test-builder
ARG AFTERGLOW_CRYPTO_REF=""

COPY tests/ ./tests/
RUN uv sync --frozen --all-extras
RUN if [ -n "$AFTERGLOW_CRYPTO_REF" ]; then \
    uv pip install --python /app/.venv/bin/python --reinstall-package afterglow-crypto --no-deps "afterglow-crypto @ git+https://github.com/openstack-afterglow/afterglow-crypto.git@$AFTERGLOW_CRYPTO_REF"; \
    fi
FROM python:3.12-slim AS lumen-runtime

WORKDIR /app

COPY --from=lumen-builder /app/.venv /app/.venv
COPY pyproject.toml uv.lock LICENSE ./
COPY lumen/ ./lumen/
COPY lumen_console/ ./lumen_console/

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && python -m compileall -q lumen \
    && adduser --disabled-password --gecos "" appuser \
    && adduser appuser root \
    && mkdir -p /data /seed \
    && chown appuser:appuser /data /seed \
    && chown -R appuser:appuser /app

ENV PATH="/app/.venv/bin:$PATH"

USER appuser

FROM lumen-runtime AS lumen-api
CMD ["uvicorn", "lumen.main:app", "--host", "0.0.0.0", "--port", "8012"]

FROM lumen-runtime AS lumen-worker
CMD ["python", "-m", "lumen.worker"]

FROM python:3.12-slim AS lumen-test

WORKDIR /app

COPY --from=lumen-test-builder /app/.venv /app/.venv
COPY pyproject.toml uv.lock LICENSE ./
COPY lumen/ ./lumen/
COPY lumen_console/ ./lumen_console/
COPY tests/ ./tests/

RUN apt-get update && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/* \
    && python -m compileall -q lumen tests \
    && adduser --disabled-password --gecos "" appuser \
    && adduser appuser root \
    && mkdir -p /data /seed \
    && chown appuser:appuser /data /seed \
    && chown -R appuser:appuser /app

ENV PATH="/app/.venv/bin:$PATH"

USER appuser

CMD ["pytest", "-m", "system", "tests/system"]
