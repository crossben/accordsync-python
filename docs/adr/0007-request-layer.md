# ADR-Y07: a framework-agnostic request layer, `AccordServer.handle()`

**Status:** accepted

## Decision

- All HTTP behaviour of `@accordsync/server`'s Hono app (`app.ts`) lives in one method:
  `AccordServer(definition, pool).handle(method, path, query, headers, body) -> Response(status,
  headers, body)`. It does routing, `Accord-Protocol: 1` on every response, CORS (Hono's `cors()`
  behaviour: origin allow-list, preflight 204, `Max-Age` 600, exposes `Accord-Protocol`), the
  body limit (413 from `Content-Length` or the body itself, before auth, as Hono's `bodyLimit`
  runs first), auth (401), device id (400), rate limits (429 + `Retry-After` in whole seconds),
  device ownership (403), malformed requests (400) and `{ "error": ... }` bodies.
  `AccordServer.wsgi` is a WSGI app over it. The FastAPI and Django packages (Y5) only translate
  requests; they must not add behaviour.
- Query numbers are read with JavaScript's `Number()` semantics (`jsnum.py`): `cursor=` is 0,
  `cursor=1e3` is 1000, `cursor=1.5` is a 400, exactly as the TypeScript server answers.
- Response bodies are written with `canonical_json` (JavaScript number formatting, lone
  surrogates escaped like `JSON.stringify`); jsonb parameters too, so PostgreSQL receives the same
  text as from the TypeScript server.
- Database access: a `psycopg_pool.ConnectionPool` (default 20, like the TypeScript pool) of
  autocommit connections; every multi-statement unit is an explicit `with conn.transaction()`.
  Pushes run under a per-definition semaphore of `max_concurrent_pushes` (default 8).
- Rate limits are in-process token buckets behind a `Limiter` protocol (`take(key) -> ms`), so a
  shared limiter can replace them. `reset_rate_limits()` restarts them (the conformance reset).
- Auth uses PyJWT configured to match `jose`: `exp`/`nbf` checked when present, `iat` not
  checked, `aud` only when an audience is configured, `iss` when an issuer is; HS256 only for a
  secret; asymmetric algorithms only with a JWKS (`PyJWKClient`, keys cached 300 s).

## Known differences (none visible to clients or to the conformance suite)

- An unknown path answers `404 { "error": "not found" }` (Hono answers a plain-text 404).
- `/metrics` (Prometheus) is not implemented yet; it belongs with the framework packages (Y5).
- Errors raised by a scope function on an existing record answer 500, like the TypeScript server.
