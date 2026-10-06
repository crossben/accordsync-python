# ADR-Y05: the Python client's threading model and Python shapes

**Status:** accepted

## Decision

- **One state lock.** Every read and mutation of client state (writer, replica, outbox, cursor,
  status) and every storage commit runs under one `RLock`. The lock is **never held during network
  I/O**: a round takes the outbox batch (or the cursor) under the lock, releases it for
  `transport.push`/`pull`, and takes it again to apply the answer. This reproduces the
  interleaving of `client.ts`, where writes run between a round's awaits: a write made during a
  push is pushed by the same round's push loop, a write made during a pull stays in the outbox and
  survives a snapshot of its record (`load_snapshot(keep=pending)`, pending computed when the page
  is applied).
- **Commits are synchronous**, made under the lock, so they happen in the same order as the state
  changes they record; there is no commit queue (`client.ts`'s `#chain`) and `flush()` only waits
  for a commit in progress on another thread.
- **One round at a time.** `sync()` called while a round runs waits for it and gets its outcome
  (result or exception), like the shared promise in `client.ts`. A `sync()` from inside the
  round's own thread (a listener or a transport hook) raises `RuntimeError` instead of
  deadlocking.
- **Events are emitted without the lock held**, on the thread that caused them, after the state
  change is committed (for a write, after its commit, also when the commit fails, then the error
  is raised). Listener exceptions are logged on the `accordsync` logger and ignored.
- **Background sync** (`start()`) is one daemon thread waiting on a condition variable: first round
  at once, 50 ms after a write (only while no failures are pending), `sync_interval` after a good
  round, `min(max_backoff, min_backoff * 2^(failures-1)) * (0.5 + random()/2)` after a bad one.
  `stop()` does not wait; `close()` stops, waits for a round in flight and for the thread, then
  closes storage. Durations are seconds (floats), `now` returns milliseconds as in the core.
- **Shapes.** `Refusal`, `ConflictInfo`/`ConflictValue`, `SyncStatus`, `PushResult`/`RefusedOp`,
  `PullPage`/`ResyncRequired` and the pull items are frozen dataclasses with snake_case fields.
  `StoredMeta` keeps the TypeScript JSON keys (`deviceId`, `cursor`, `hlc`, `seq`) in `accord_meta`.
- **SQLite JSON** is compact like `JSON.stringify` but ASCII-escaped (`json.dumps` default), so lone
  surrogates (legal in Accord strings) can be stored; `JSON.parse` reads both forms, so databases
  stay interchangeable with the TypeScript adapter.

## Why

A synchronous client (ADR-Y01) has no event loop to interleave writes and rounds; threads do it
instead, and holding one lock across a network call would block every write for the length of a
request. The tests port the Dart "writes during a sync round" and "snapshot with an unpushed edit"
cases with a write injected during the transport call, plus a write from a second thread while a
push is blocked.
