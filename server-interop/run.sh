#!/usr/bin/env bash
# The mixed-server fleet: one PostgreSQL, the TypeScript reference server (Accord workspace
# sources) and the Python server on it at once, Python and TypeScript devices on both.
#   server-interop/run.sh                          (needs Docker, Node 22+, pnpm install in the app)
#   ACCORD_FLEET_SEEDS=1,2,3,4,5 server-interop/run.sh -x
# ACCORD_DATABASE_URL: a PostgreSQL to use instead of starting one (CI); the tests create and drop
#   the database `accord_fleet` on it.
# ACCORD_APP_DIR: the Accord workspace (default ../../app; CI: the accordsync checkout).
# Per seed, the tests recreate the database, migrate it with one implementation (even seeds
# TypeScript, odd Python), check the other has nothing to do, start both servers (ports 8851/8852
# TypeScript, 8853/8854 Python), run the fleet and stop them (by PID).
set -euo pipefail
cd "$(dirname "$0")"
here="$(pwd)"
export ACCORD_APP_DIR="${ACCORD_APP_DIR:-$(cd "$here/../../app" && pwd)}"

container=""
pidfile="$here/.logs/servers.pid"
cleanup() {
  # Servers the tests started and could not stop (pytest killed): by PID.
  if [[ -f "$pidfile" ]]; then
    while read -r pid; do kill "$pid" 2>/dev/null || true; done <"$pidfile"
    rm -f "$pidfile"
  fi
  if [[ -n "$container" ]]; then docker rm -f "$container" >/dev/null 2>&1 || true; fi
}
trap cleanup EXIT

if [[ -z "${ACCORD_DATABASE_URL:-}" ]]; then
  container=accord-py-fleet-pg
  port="${ACCORD_FLEET_PG_PORT:-55481}"
  docker rm -f "$container" >/dev/null 2>&1 || true
  docker run -d --name "$container" -e POSTGRES_USER=accord -e POSTGRES_PASSWORD=accord \
    -e POSTGRES_DB=accord -p "$port:5432" postgres:16-alpine >/dev/null
  for _ in $(seq 1 60); do
    docker exec "$container" pg_isready -U accord -h 127.0.0.1 >/dev/null 2>&1 && break
    sleep 0.5
  done
  export ACCORD_DATABASE_URL="postgresql://accord:accord@127.0.0.1:$port/accord"
fi

[[ -d "$ACCORD_APP_DIR/conformance/node_modules" ]] || (cd "$ACCORD_APP_DIR" && pnpm install --frozen-lockfile)

cd ..
"${UV:-uv}" run pytest server-interop/tests -s "$@"
