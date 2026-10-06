# ADR-Y09: the FastAPI and Django packages are thin adapters over `AccordServer.handle()`

**Status:** accepted

## Decision

- Both packages translate a request into `AccordServer.handle(method, path, query, headers, body)`
  and its `Response` back, nothing else (ADR-Y07). Each of the three paths (`/v1/push`,
  `/v1/pull`, `/health`) accepts every method, so `handle()` answers 404 and the CORS preflight
  204 exactly like the TypeScript server, instead of the framework's 405.
- The body is read from the raw stream, at most one byte past `limits.max_body_bytes`, and passed
  as bytes: the 413 and JSON parsing are `handle()`'s. Django reads `request.read()`, not
  `request.body`, so `DATA_UPLOAD_MAX_MEMORY_SIZE` does not apply.
- FastAPI: plain `def` endpoints in the thread pool (ADR-Y01); only the body read is an `async`
  dependency. Django: views are `csrf_exempt` and `non_atomic_requests` (the push transaction is
  explicit on the server's own connection; `ATOMIC_REQUESTS` must not wrap it), and need no
  session or auth middleware.
- Compaction: when `compaction.interval_ms > 0`, each serving process compacts on that interval on
  a daemon thread, like the TypeScript `serve` timer: FastAPI starts it in the router's lifespan,
  Django on the first request to an Accord view (so management commands never start it). Both can
  turn it off (`schedule_compaction=False`, `ACCORD_SCHEDULE_COMPACTION = False`). With several
  worker processes each compacts; that is safe (compaction takes the feed lock exclusively) but
  redundant, so multi-process deployments should turn it off and run `accord_compact` /
  `python -m accordsync_fastapi compact` from cron.
- Rate limits stay in process (ADR-Y07). The examples therefore serve from one process with a
  thread pool (uvicorn's for FastAPI, waitress with 32 threads for Django), so the test-only
  control API's reset clears the limits the sync endpoints use.
- The examples use `tools/conformance_server.py`'s definition and control API (reading
  `contract/conformance/profile.json`) rather than a second copy.

## Known differences

- A path outside the three answers the framework's own 404 (FastAPI `{"detail": ...}`, Django's
  page), not `{ "error": "not found" }`. The suite does not test it.
- Django merges repeated response headers into one comma-separated header (its `HttpResponse`
  holds one value per name); `handle()` only repeats `Vary`, where this is equivalent.
