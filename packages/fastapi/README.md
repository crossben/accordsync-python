# accordsync-fastapi

The Accord sync server for FastAPI: a router with `POST /v1/push`, `GET /v1/pull` and
`GET /health`, and a CLI for migrations and compaction. All behaviour (auth, limits, CORS, rate
limits, status codes, `Accord-Protocol` and `Retry-After` headers) comes from
`accordsync-server`; this package only routes to it. Part of [Accord](https://accord.benhattab.pro).

## Install

```sh
pip install accordsync-fastapi uvicorn
```

## Use

```python
from accordsync_core import define_schema, lww
from accordsync_server import Access, Auth, define_server
from accordsync_fastapi import accord_router
from fastapi import FastAPI

server = define_server(
    schema=define_schema({"note": {"owner": lww(), "title": lww()}}),
    scopes={"note": lambda r: [f"user:{r.fields.get('owner')}"]},
    access=lambda claims: Access(read=[f"user:{claims['sub']}"], write=[f"user:{claims['sub']}"]),
    auth=Auth.jwks("https://auth.example/.well-known/jwks.json", issuer="https://auth.example"),
)

app = FastAPI()
app.include_router(accord_router(server, database_url="postgresql://...", prefix="/sync"))
```

Given a definition, the router owns a psycopg pool (`pool_size`, default 20): it opens it in the
application's lifespan and closes it at shutdown. You can instead pass an
`AccordServer(definition, pool)` whose pool you manage; `router_server(router)` returns the server
behind a router. Endpoints are plain `def` functions run in FastAPI's thread pool.

Run the migrations before serving:

```sh
ACCORD_DATABASE_URL=postgresql://... python -m accordsync_fastapi migrate
```

## Compaction

With `compaction.interval_ms > 0` in the definition (default one hour), the router's lifespan
compacts on that interval in a background thread of each process. With several worker processes,
pass `schedule_compaction=False` and run compaction from cron instead:

```sh
ACCORD_DATABASE_URL=postgresql://... python -m accordsync_fastapi compact --definition myapp.sync:server
```

## Notes

- Rate limits are per process (in memory). With several workers, each has its own buckets.
- `/health` touches the database and answers 503 when it is unreachable.
- Put TLS and request-size limits of your proxy in front as usual; the body limit
  (`limits.max_body_bytes`) is enforced while reading the body.
- `examples/fastapi_app` in this repository serves the conformance profile and passes the shared
  server conformance suite (63/63). Its control API is for tests only (`ACCORD_CONTROL=1`).
