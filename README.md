# Accord for Python

**Offline-first sync that stays correct when the network lies**, for Python programs and for
FastAPI and Django backends.

Apps keep working with no connection. When it comes back, every device ends up with the same data:
changes merge by rules you declare per field, counters never lose an increment, and conflicting
decisions are kept for your app to settle instead of being guessed.

Two things live here:

- a **client**, so Python programs write offline and sync like any other Accord device;
- a **server** for FastAPI and Django, with the same protocol, merge rules and PostgreSQL schema as
  [`@accordsync/server`](https://github.com/crossben/accordsync), so every existing client
  (TypeScript, React Native, Flutter) syncs with it unchanged.

> **Status: in development.** Not published on PyPI yet. Website and docs:
> [accord.benhattab.pro](https://accord.benhattab.pro).

## Packages

| Package | What it does |
| --- | --- |
| `accordsync-core` | The merge core: hybrid logical clocks, operations, `lww`, `counter`, `set` and `conflict`. |
| `accordsync` | The client: local-first writes, background sync, conflicts and refusals; SQLite storage. |
| `accordsync-server` | The sync server on PostgreSQL, framework-agnostic: push, pull, scopes, auth, compaction. |
| `accordsync-fastapi` | FastAPI integration: router, settings, CLI. |
| `accordsync-django` | Django integration: app, URLs, management commands. |

## Develop

Requires Python 3.11+ and [uv](https://docs.astral.sh/uv/).

```sh
uv sync
uv run ruff check . && uv run ruff format --check .
uv run mypy
uv run pytest
```

`contract/` holds the golden vectors and protocol schemas from the Accord repository; this
implementation must pass them. Refresh it with `python tools/sync_contract.py` (reads `../app`, or
`ACCORD_APP_DIR`).

`interop/run.sh` runs the Python client against the real Accord server (from npm, on PostgreSQL in
Docker), alone and in a mixed fleet with TypeScript devices over a lossy network. Needs Docker and
Node 22+; see [interop/README.md](interop/README.md).

## Licence

[Apache-2.0](LICENSE).
