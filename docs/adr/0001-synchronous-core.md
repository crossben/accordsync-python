# ADR-Y01: synchronous client and server, one code path each

**Status:** accepted

## Decision

- The **server** is one synchronous implementation on `psycopg` 3. Django calls it directly; the
  FastAPI router declares plain `def` endpoints, which FastAPI runs in its thread pool.
- The **client** has a synchronous API. `start()` runs background sync on a daemon thread; `sync()`
  can also be called directly. An `asyncio` wrapper can come later without changing the core.

## Why

The push transaction depends on exact locking (advisory lock, rows locked `FOR UPDATE` in sorted
order, pulls below `accord_horizon()`, ADR-0010 in the Accord repository). One synchronous code path
is easier to make and keep correct than two, and Django is synchronous at heart. The client's users
(scripts, desktop tools, kiosks, data jobs) mostly run synchronous code. Revisit only if a benchmark
shows the thread pool limits the server.
