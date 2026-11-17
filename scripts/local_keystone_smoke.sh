#!/usr/bin/env bash
# Loopback Lumen API for browser/BFF smoke tests: real Uvicorn app, fresh disposable MariaDB+Redis (Docker),
# and the SYNTHETIC Keystone from tests/system/fake_keystone.py. No production credential is read or sent.
#
#   scripts/local_keystone_smoke.sh up     # prints the connection contract as JSON
#   scripts/local_keystone_smoke.sh down
#
# Env: LUMEN_SMOKE_API_PORT (default 18012), LUMEN_SMOKE_KEYSTONE_PORT (default 15000), LUMEN_SMOKE_PROJECT.
set -euo pipefail
cd "$(dirname "$0")/.."
PROJECT="${LUMEN_SMOKE_PROJECT:-lumen-smoke}"
API_PORT="${LUMEN_SMOKE_API_PORT:-18012}"
KS_PORT="${LUMEN_SMOKE_KEYSTONE_PORT:-15000}"
STATE="${TMPDIR:-/tmp}/${PROJECT}"
COMPOSE=(docker compose -p "$PROJECT" -f docker-compose.system.yml)
RUN=(uv run --extra service)

case "${1:-up}" in
  down)
    for name in api keystone; do
      [ -f "$STATE/$name.pid" ] && kill "$(cat "$STATE/$name.pid")" 2>/dev/null || true
    done
    "${COMPOSE[@]}" down -v --remove-orphans --timeout 1 || true
    rm -rf "$STATE"
    ;;
  up)
    mkdir -p "$STATE"
    export MARIADB_PORT="${MARIADB_PORT:-$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')}"
    export REDIS_PORT="${REDIS_PORT:-$(python3 -c 'import socket;s=socket.socket();s.bind(("127.0.0.1",0));print(s.getsockname()[1])')}"
    export COMPOSE_PROFILES="" RUNTIME_CONFIG='{"enabled":false}'
    "${COMPOSE[@]}" up -d --wait --wait-timeout 120 mariadb redis >&2
    PORT="$KS_PORT" nohup "${RUN[@]}" python tests/system/fake_keystone.py >"$STATE/keystone.log" 2>&1 &
    echo $! >"$STATE/keystone.pid"
    export DATABASE_URL="mysql+aiomysql://lumen:lumen@127.0.0.1:${MARIADB_PORT}/lumen"
    export REDIS_URL="redis://127.0.0.1:${REDIS_PORT}/0"
    export KEYSTONE_AUTH_URL="http://127.0.0.1:${KS_PORT}/v3" KEYSTONE_ADMIN_USERNAME=synthetic-directory
    export KEYSTONE_ADMIN_PASSWORD=synthetic-directory-password KEYSTONE_ADMIN_PROJECT=synthetic-directory-project
    export KEYSTONE_DOMAIN=Default AFTERGLOW_ENV=development
    export LUMEN_ENCRYPTION_KEY=0123456789abcdef0123456789abcdef0123456789abcdef0123456789abcdef
    export CHAT_API_HOSTS=localhost,127.0.0.1 FRONTEND_BASE_URL="${LUMEN_SMOKE_FRONTEND_ORIGIN:-http://127.0.0.1:3080}"
    export CHAT_MCP_OAUTH_CALLBACK_URL="http://127.0.0.1:${API_PORT}/api/v1/chat/mcp-oauth/callback"
    export OPENAI_API_KEY=fake-provider-key CHAT_EXECUTION_PROTOCOL_VERSION=1 RUNTIME_CONFIG='{"enabled":false}'
    export OSLO_POLICY_POLICY_FILE="$PWD/lumen/policy.yaml.sample"
    for _ in $(seq 1 50); do curl -fs "http://127.0.0.1:${KS_PORT}/health" >/dev/null && break; sleep 0.2; done
    "${RUN[@]}" lumen-migrate --apply >&2
    (cd runtime/bifrost && go build -o "$STATE/lumen-bifrost" ./cmd/lumen-bifrost)
    export BIFROST_EXECUTABLE="$STATE/lumen-bifrost"
    nohup "${RUN[@]}" uvicorn lumen.main:app --host 127.0.0.1 --port "$API_PORT" >"$STATE/api.log" 2>&1 &
    echo $! >"$STATE/api.pid"
    for _ in $(seq 1 100); do curl -fs "http://127.0.0.1:${API_PORT}/v1/health" >/dev/null && break; sleep 0.3; done
    curl -fs "http://127.0.0.1:${API_PORT}/v1/health" >/dev/null || { tail -40 "$STATE/api.log" >&2; exit 1; }
    cat <<JSON
{"lumen_url":"http://127.0.0.1:${API_PORT}","keystone_url":"http://127.0.0.1:${KS_PORT}/v3",
 "project_id":"system-project","user_id":"system-owner","owner_token":"synthetic-owner-token",
 "control":{"url":"http://127.0.0.1:${KS_PORT}/_control/configure","header":"X-Test-Control-Token","value":"synthetic-directory-control"},
 "domain_id":"default","state_dir":"${STATE}"}
JSON
    ;;
  *) echo "usage: $0 up|down" >&2; exit 2 ;;
esac
