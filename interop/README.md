# Interop tests

Python and TypeScript Accord clients against the real server.

- `tests/test_http.py`: the Python client (`HttpTransport`) against `@accordsync/server`: conflicts,
  refusals with the server's reason, scope exits and deltas, compaction snapshots (with a restart
  from `SqliteStorage`), `device_seq`, "op id already used", HTTP errors.
- `tests/test_mixed_fleet.py`: two Python devices and two TypeScript devices (`@accordsync/client`,
  driven by `node/ts-client.mjs`) edit the same records through a network that loses requests and
  responses, with a compaction mid-run. After the network heals, every device must hold
  byte-identical canonical snapshots.

The server and TypeScript client are the published npm packages, pinned in `node/package.json`.

```sh
interop/run.sh                               # needs Docker, Node 22+ and uv
ACCORD_INTEROP_SEEDS=1,2,3,4,5 interop/run.sh
interop/run.sh -k fleet -x                   # extra arguments go to pytest
```

`run.sh` starts PostgreSQL in Docker on host port 55433 (unless `ACCORD_DATABASE_URL` is set), the
server on port 8797, and stops both when it exits. `ACCORD_PORT` and `ACCORD_TEST_PORT` change the
server ports. These tests are not part of the root `uv run pytest`.

`node/server.mjs` also listens on a second port (8798) for test-only routes: sign a token, reset the
database, run compaction. It exists for these tests only.
