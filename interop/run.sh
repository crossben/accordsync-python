#!/usr/bin/env bash
# Runs the interop tests: PostgreSQL in Docker, the real Accord server from npm, then pytest.
#   interop/run.sh                       (from the repository root or interop/)
#   ACCORD_INTEROP_SEEDS=1,2,3,4,5 interop/run.sh
# Set ACCORD_DATABASE_URL to use a PostgreSQL that is already running (CI does).
# ACCORD_PORT / ACCORD_TEST_PORT pick the server ports (default 8797 / 8798).
set -euo pipefail
cd "$(dirname "$0")"
here="$(pwd)"

export ACCORD_PORT="${ACCORD_PORT:-8797}"
export ACCORD_TEST_PORT="${ACCORD_TEST_PORT:-8798}"
export ACCORD_URL="http://localhost:$ACCORD_PORT"
export ACCORD_TEST_URL="http://localhost:$ACCORD_TEST_PORT"

server=""
compose=""
cleanup() {
  if [[ -n "$server" ]]; then kill "$server" 2>/dev/null || true; wait "$server" 2>/dev/null || true; fi
  if [[ -n "$compose" ]]; then docker compose -f "$here/docker-compose.yml" down >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

if [[ -z "${ACCORD_DATABASE_URL:-}" ]]; then
  compose=1
  docker compose up -d --wait
  export ACCORD_DATABASE_URL=postgres://accord:accord@localhost:55433/accord
fi

(cd node && npm ci --no-audit --no-fund)
node node/server.mjs &
server=$!
for _ in $(seq 1 60); do curl -sf "$ACCORD_URL/health" >/dev/null && break; sleep 0.5; done
curl -sf "$ACCORD_URL/health" >/dev/null || { echo "server did not start" >&2; exit 1; }

cd ..
"${UV:-uv}" run pytest interop/tests "$@"
