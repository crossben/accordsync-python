# ADR-Y10: Django uses its own psycopg pool, not the ORM's connections

**Status:** accepted

## Decision

- `accordsync_django` opens one `psycopg_pool.ConnectionPool` per process (`create_pool`, size
  `ACCORD_POOL_SIZE`, default 20) on the first request, closed at exit. The URL is
  `ACCORD_DATABASE_URL` (setting, then environment), else built from `DATABASES["default"]`, which
  must then be `django.db.backends.postgresql`.
- Django's ORM connections are never used by the sync server.

## Why

The push transaction needs autocommit connections with explicit `BEGIN ... COMMIT`, a transaction
advisory lock and rows locked `FOR UPDATE` (ADR-0010 in the Accord repository). Django's connection
is per thread, may be inside `ATOMIC_REQUESTS` or a caller's `atomic()`, and is closed by
`CONN_MAX_AGE` handling between requests; sharing it would let Django's transaction state change
the sync server's. One code path (the server's pool) is the same as under FastAPI and the
conformance server. The cost is a second set of connections to PostgreSQL; size the pool for it.
