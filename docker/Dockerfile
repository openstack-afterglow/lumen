ARG AFTERGLOW_CRYPTO_REF=""

FROM python:3.12-slim AS lumen-builder
ARG AFTERGLOW_CRYPTO_REF=""

WORKDIR /app

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

RUN apt-get update && apt-get install -y --no-install-recommends \
    build-essential \
    git \
    && rm -rf /var/lib/apt/lists/*

# Workspace manifests first so the frozen dependency layer caches independently of sources.
COPY pyproject.toml uv.lock LICENSE ./
COPY packages/lumen-plugin-api/pyproject.toml ./packages/lumen-plugin-api/pyproject.toml
COPY plugins/database-mariadb/pyproject.toml ./plugins/database-mariadb/pyproject.toml
COPY plugins/memory-default/pyproject.toml ./plugins/memory-default/pyproject.toml
COPY plugins/tools-default/pyproject.toml ./plugins/tools-default/pyproject.toml
COPY plugins/skills-default/pyproject.toml ./plugins/skills-default/pyproject.toml
COPY plugins/mcp-default/pyproject.toml ./plugins/mcp-default/pyproject.toml
RUN uv sync --locked --extra service --no-dev --no-install-project --no-install-workspace

COPY lumen/ ./lumen/
COPY lumen_console/ ./lumen_console/
COPY packages/lumen-plugin-api/ ./packages/lumen-plugin-api/
COPY plugins/ ./plugins/
RUN uv sync --locked --extra service --no-dev
RUN if [ -n "$AFTERGLOW_CRYPTO_REF" ]; then \
    uv pip install --python /app/.venv/bin/python --reinstall-package afterglow-crypto --no-deps "afterglow-crypto @ git+https://github.com/openstack-afterglow/afterglow-crypto.git@$AFTERGLOW_CRYPTO_REF"; \
    fi

FROM lumen-builder AS lumen-test-builder
ARG AFTERGLOW_CRYPTO_REF=""

COPY tests/ ./tests/
RUN uv sync --locked --extra service --extra dev
RUN if [ -n "$AFTERGLOW_CRYPTO_REF" ]; then \
    uv pip install --python /app/.venv/bin/python --reinstall-package afterglow-crypto --no-deps "afterglow-crypto @ git+https://github.com/openstack-afterglow/afterglow-crypto.git@$AFTERGLOW_CRYPTO_REF"; \
    fi

FROM python:3.12-slim AS lumen-runtime

WORKDIR /app

COPY --from=lumen-builder /app/.venv /app/.venv
COPY pyproject.toml uv.lock LICENSE ./
COPY lumen/ ./lumen/
COPY lumen_console/ ./lumen_console/
COPY packages/lumen-plugin-api/ ./packages/lumen-plugin-api/
COPY plugins/ ./plugins/

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

# Import the actual migration entry point without opening a database connection.
# This catches missing workspace distributions or editable sources before deployment.
RUN python -m lumen.scripts.migrate --help

FROM lumen-runtime AS lumen-api
CMD ["uvicorn", "lumen.main:app", "--host", "0.0.0.0", "--port", "8012"]

FROM lumen-runtime AS lumen-worker
CMD ["python", "-m", "lumen.worker"]

FROM lumen-runtime AS lumen-controller
# The controller alone receives cloud application credentials; it shares the trusted
# runtime stage but never the sandbox stage below.
CMD ["python", "-m", "lumen.controller"]

FROM python:3.12-slim AS lumen-test

WORKDIR /app

COPY --from=lumen-test-builder /app/.venv /app/.venv
COPY pyproject.toml uv.lock LICENSE ./
COPY lumen/ ./lumen/
COPY lumen_console/ ./lumen_console/
COPY packages/lumen-plugin-api/ ./packages/lumen-plugin-api/
COPY plugins/ ./plugins/
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

# ---------------------------------------------------------------------------
# Standalone sandbox image: only the lumen-sandbox wheel, never the service extra,
# credentials or the trusted runtime stage. Contract: packages/lumen-sandbox/IMAGE.md.
# ---------------------------------------------------------------------------
FROM python:3.12-slim AS lumen-sandbox-builder

WORKDIR /build
COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/
COPY packages/lumen-sandbox/ ./packages/lumen-sandbox/
RUN uv build --wheel --out-dir /wheels ./packages/lumen-sandbox

FROM python:3.12-slim AS lumen-sandbox
ARG TARGETARCH
ARG NODE_VERSION=24.21.0
ARG NODE_SHA256_AMD64=fd8e59d5a511510f6a298afb548f18c7d2b1be404d8b4a27d94fbe49f56cb2d6
ARG NODE_SHA256_ARM64=6ad1325edbdb5649c379b75a237147a666c95d4f9ae8d340fef2d1575d289ad2

RUN apt-get update && apt-get install -y --no-install-recommends \
    bubblewrap \
    ca-certificates \
    curl \
    mount \
    nftables \
    xz-utils \
    && rm -rf /var/lib/apt/lists/*

# Pinned Node LTS with a per-architecture checksum; an unsupported architecture fails loudly.
RUN set -eu; \
    case "$TARGETARCH" in \
      amd64) node_arch=x64; expected="$NODE_SHA256_AMD64" ;; \
      arm64) node_arch=arm64; expected="$NODE_SHA256_ARM64" ;; \
      *) echo "unsupported architecture: $TARGETARCH" >&2; exit 1 ;; \
    esac; \
    archive="node-v${NODE_VERSION}-linux-${node_arch}.tar.xz"; \
    curl -fsSL "https://nodejs.org/dist/v${NODE_VERSION}/${archive}" -o "/tmp/${archive}"; \
    echo "${expected}  /tmp/${archive}" | sha256sum -c -; \
    mkdir -p /opt/node; \
    tar -xJf "/tmp/${archive}" -C /opt/node --strip-components=1; \
    rm -f "/tmp/${archive}"; \
    ln -s /opt/node/bin/node /usr/bin/node; \
    ln -sf /usr/local/bin/python3.12 /usr/bin/python3.12; \
    /usr/bin/node --version | grep -qx "v${NODE_VERSION}"; \
    /usr/bin/python3.12 --version

COPY --from=lumen-sandbox-builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/lumen_sandbox-*.whl \
    && rm -rf /wheels \
    && groupadd --system --gid 4300 sandbox-workload \
    && useradd --system --uid 4300 --gid 4300 --no-create-home --shell /usr/sbin/nologin sandbox-workload \
    && mkdir -p /var/lib/lumen-sandbox /run/lumen-sandbox/workspaces \
    && chmod 0700 /var/lib/lumen-sandbox \
    && chmod 0711 /run/lumen-sandbox /run/lumen-sandbox/workspaces

ENV SANDBOX_STATE_DIR=/var/lib/lumen-sandbox \
    SANDBOX_WORKSPACE_DIR=/run/lumen-sandbox/workspaces/current \
    SANDBOX_CGROUP_PARENT=/sys/fs/cgroup/lumen-sandbox \
    SANDBOX_WORKLOAD_UID=4300 \
    SANDBOX_WORKLOAD_GID=4300 \
    SANDBOX_NODE_MAJOR=24 \
    SANDBOX_NODE_VERSION=${NODE_VERSION} \
    SANDBOX_BOOTSTRAP_FILE=/var/lib/lumen-sandbox/bootstrap/token \
    SANDBOX_PORT=8013

# The manager must run as root to create namespaces/cgroups; workloads never do.
USER root
CMD ["lumen-sandbox"]
