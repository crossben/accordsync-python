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

> **Status: v0.3.0.** Pre-1.0: the API may still change between minor versions. Website and docs:
> [accord.benhattab.pro](https://accord.benhattab.pro/docs/python/).

```sh
pip install accordsync            # the client
pip install accordsync-fastapi    # the server, for FastAPI
pip install accordsync-django     # the server, for Django
pip install accordsync-server     # the server, for WSGI or another framework
```

## Packages

| Package | What it does |
| --- | --- |
| `accordsync-core` | The merge core: hybrid logical clocks, operations, `lww`, `counter`, `set` and `conflict`. |
| `accordsync` | The client: local-first writes, background sync, conflicts and refusals; SQLite storage. |
| `accordsync-server` | The sync server on PostgreSQL, framework-agnostic: push, pull, scopes, auth, compaction. |
| `accordsync-fastapi` | FastAPI integration: router, settings, CLI. |
| `accordsync-django` | Django integration: app, URLs, management commands. |

## How compatibility is proven

- `contract/` holds the golden vectors and protocol schemas from the Accord repository: the core
  passes every vector in every delivery order, and reproduces the TypeScript core's random scenarios
  byte for byte.
- CI runs Accord's black-box HTTP conformance suite (`conformance/` in the Accord repository)
  against the Python server and against the FastAPI and Django example apps.
- `interop/` runs the Python client against the TypeScript server, alone and with TypeScript devices;
  `server-interop/` runs a mixed-server fleet: the TypeScript and Python servers on one PostgreSQL
  database at the same time, with Python and TypeScript devices sending every request to either, over
  a network that loses requests and responses. Every device must end with identical data.

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
