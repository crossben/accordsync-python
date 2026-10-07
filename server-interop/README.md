# Mixed-server fleet

plan-python.md §0 point 4: one PostgreSQL database with the TypeScript reference server
(`app/conformance/reference-server.ts`, Accord workspace sources) and the Python server
(`tools/conformance_server.py`) running on it at once, both with the conformance profile.

For each seed (`tests/test_mixed_server_fleet.py`):

- the database is recreated and migrated by one implementation (even seeds TypeScript, odd
  Python); the other must find nothing to do (same Kysely ledger, ADR-Y03/Y06);
- two Python devices (`accordsync`, `MemoryStorage`) and two TypeScript devices
  (`node/ts-client.mts`: `@accordsync/client` from the workspace sources) for two users send every
  request to a server picked at random, through a network that loses 25% (requests, and responses
  after the server applied them); they act four at a time, with seeded edits and awkward values;
- mid-run: heal, settle, a probe op whose ack was "lost", compaction through a random server's
  control API (must fold), the probe retried on both servers (acked) and tampered (refused), back
  to the lossy network;
- later: raw probe devices pull every scope event (a record moving in and out of a zone through
  each server, a token gaining and losing a zone) one from each server and must get identical
  items; moussa's devices get the token with the extra zone;
- end: heal, settle; every device holds the same canonical snapshot, nothing pending, equal to
  what the database holds (each `records` row must also agree with its feed), and both servers
  served pushes and pulls (counts printed and appended to `.logs/summary.jsonl`).

`test_lone_surrogate_in_an_op_value_is_answered_alike` sends `"\ud800"` inside an op value to
each server: both refuse that op (`malformed op: lone surrogate in op.value`, since PostgreSQL's
jsonb cannot store it), apply the rest of the batch and store nothing of it.
`test_a_lost_scope_delta_is_sent_again` loses the answer that carries a scope delta and checks the
retry carries it again (ADR-0011, update of 2026-10-07).

```sh
server-interop/run.sh                          # Docker, Node 22+, uv; pnpm install done in app/
ACCORD_FLEET_SEEDS=1,2,3,4,5 server-interop/run.sh -x
```

`run.sh` starts PostgreSQL in Docker (`accord-py-fleet-pg`, host port 55481) unless
`ACCORD_DATABASE_URL` is set, and removes it at the end. `ACCORD_APP_DIR` is the Accord workspace
(default `../../app`). Ports: TypeScript 8851/8852, Python 8853/8854 (`ACCORD_TS_PORT`,
`ACCORD_TS_CONTROL_PORT`, `ACCORD_PY_PORT`, `ACCORD_PY_CONTROL_PORT`). Server logs go to `.logs/`.
Not part of the root `uv run pytest`.
