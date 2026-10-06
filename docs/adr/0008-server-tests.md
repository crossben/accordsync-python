# ADR-Y08: how the server is tested

**Status:** accepted

## Decision

- **Protocol behaviour** is proven by the shared conformance suite (`app/conformance` in the Accord
  repository), run against `tools/conformance_server.py` (the profile on one port, the control API
  on another, a threaded WSGI server so concurrency tests really run concurrently). CI job
  `conformance`.
- **pytest** covers what the suite cannot reach: definition validation, auth (HS256 and a local
  JWKS), the token bucket, `Number()` parsing, `op_hash` against hashes computed by the TypeScript
  server (non-ASCII, astral and lone surrogates), the migration ledger in both directions, CORS,
  WSGI and the CLI.
- Tests needing PostgreSQL **skip** unless `ACCORD_TEST_DATABASE_URL` names a server where they may
  create databases (each test gets a fresh one, dropped afterwards). They do not start a container
  themselves: the root `uv run pytest` stays fast and Docker-free; CI provides a PostgreSQL service.
  Tests that run the TypeScript server's code (`tests/ts/ts_tool.mts`) also need `ACCORD_APP_DIR`
  (an installed Accord checkout) and `node`; otherwise they skip.
- **Planted bugs** (each caught by the suite, see the Y4 report): no record lock; unsorted record
  locks (deadlocks); pulls ignoring `accord_horizon()`; no scope check for new records; a reused
  compacted id acked without comparing `op_hash`; a dropped exit item (feed and scope delta).
