# accordsync-server

**The [Accord](https://accord.benhattab.pro) sync server for Python, on PostgreSQL.**

Framework-agnostic: push and pull, scopes, JWT auth, limits, rate limits, CORS and compaction behind
one `handle()` call per request, plus a WSGI application. It speaks the same protocol, merges by the
same rules and uses the same PostgreSQL schema as
[`@accordsync/server`](https://www.npmjs.com/package/@accordsync/server), so the TypeScript, React
Native, Flutter and Python clients sync with it unchanged.

With FastAPI or Django, use [`accordsync-fastapi`](https://pypi.org/project/accordsync-fastapi/) or
[`accordsync-django`](https://pypi.org/project/accordsync-django/): they route to this package. This
page is for WSGI, or for another framework.

## Install

```sh
pip install accordsync-server
```

Python 3.11+ and PostgreSQL (the conformance suite and the example apps use PostgreSQL 16). It
connects with psycopg 3 and its connection pool.

## Define the server

`define_server()` takes the same parts as `defineServer` in TypeScript: the schema, a scope function
per record type, the access a user gets from their JWT claims, and how tokens are checked. It checks
at once that every record type has a scope function.

```python
# myapp/sync.py
from accordsync_core import conflict, counter, define_schema, lww, set_
from accordsync_server import Access, Auth, ScopedRecord, define_server

schema = define_schema(
    {"dossier": {"agent": lww(), "visits": counter(), "docs": set_(), "status": conflict()}}
)


def dossier_scope(record: ScopedRecord) -> list[str]:
    """The scope keys of a record, from its current fields."""
    agent = record.fields.get("agent")
    return [f"agent:{agent}"] if isinstance(agent, str) else []


server = define_server(
    schema=schema,
    scopes={"dossier": dossier_scope},
    # The scope keys a user may read and write, from their verified JWT claims.
    access=lambda claims: Access(read=[f"agent:{claims['sub']}"], write=[f"agent:{claims['sub']}"]),
    auth=Auth.jwks(
        "https://auth.example.com/.well-known/jwks.json",
        issuer="https://auth.example.com/",
        audience="accord",
    ),
)
```

Optional arguments: `cors` (browser origins allowed to call the API), `rate_limit` (a `RateLimits`;
default 600 requests a minute per device and 1 800 per user; `False` turns it off), `compaction` (a
`Compaction`: device TTL, interval, minimum ops) and `limits` (a `Limits`: body size, concurrent
pushes, ops per push, pull page size, scope delta size, clock skew). `Auth.hs256(secret)` is for
development and tests.

## Serve it

`AccordServer(definition, pool)` serves `GET /health`, `POST /v1/push` and `GET /v1/pull`.
`server.handle(method, path, query, headers, body)` returns a `Response(status, headers, body)` for
any framework; `server.wsgi` is a ready WSGI application:

```python
# myapp/wsgi.py
import os

from accordsync_server import AccordServer, create_pool

from myapp.sync import server as definition

app = AccordServer(definition, create_pool(os.environ["ACCORD_DATABASE_URL"])).wsgi
```

`create_pool(url, size=20)` opens a psycopg pool of up to `size` connections. Serve `app` with a
threaded WSGI server (waitress, or gunicorn with `--threads`): each request holds a thread while it
waits on PostgreSQL. `ACCORD_DATABASE_URL` is a URL like `postgresql://user:password@host:5432/db`.

## Migrations and compaction

```sh
ACCORD_DATABASE_URL=postgresql://… python -m accordsync_server migrate
ACCORD_DATABASE_URL=postgresql://… python -m accordsync_server compact --definition myapp.sync:server
```

Run `compact` from cron at the definition's `compaction.interval_ms` (default hourly). It takes
PostgreSQL's exclusive advisory lock, so overlapping runs, or several servers, do not conflict. In
code: `migrate(url)` and `compact(pool, definition)`.

## One database, any Accord server

The migrations are the TypeScript server's, recorded in the same ledger table (`kysely_migration`):
a database migrated by `@accordsync/server` is up to date here, and the other way round. One database
can be served by TypeScript and Python servers at the same time, with clients sent to either. The
repository's `server-interop/` harness does exactly that: Python and TypeScript devices send every
request to a randomly chosen server, through a network that loses requests and responses, and must
end with identical data. This server, and the repository's FastAPI and Django example apps, also
pass Accord's black-box HTTP conformance suite, the same one the TypeScript server passes.

## Security notes

- Requests authenticate with `Authorization: Bearer <jwt>`. In production use `Auth.jwks()` with an
  issuer and an audience.
- Rate limits are kept in memory, per process: with several worker processes, each has its own
  buckets.
- Request bodies above `limits.max_body_bytes` (default 5 MiB) get 413; the WSGI app stops reading
  past the limit.
- CORS for the sync API is the definition's `cors` list, answered by the server.
- What a user may read and write is decided only by your `access` function and scope functions.
  See [docs/security.md](https://github.com/crossben/accordsync/blob/main/docs/security.md).

Docs: [accord.benhattab.pro/docs/python](https://accord.benhattab.pro/docs/python/) ·
Source: [crossben/accordsync-python](https://github.com/crossben/accordsync-python) · Licence: Apache-2.0
