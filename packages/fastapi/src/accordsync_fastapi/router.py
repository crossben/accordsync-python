"""`accord_router(...)`: the Accord endpoints as a FastAPI `APIRouter`.

The router only translates requests: every status, header and body comes from
`AccordServer.handle()` (ADR-Y07). Endpoints are plain `def`, run in FastAPI's thread pool
(ADR-Y01); only reading the body is async, and it stops one byte past the body limit.
"""

from __future__ import annotations

import logging
import threading
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from typing import Any

from accordsync_server import AccordServer, ServerDefinition, compact, create_pool
from fastapi import APIRouter, Depends, Request
from fastapi import Response as FastAPIResponse

log = logging.getLogger("accordsync_fastapi")

# Every method goes to `handle()`, which answers 404 / 204 (CORS preflight) exactly like the
# TypeScript server, instead of FastAPI's 405.
METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"]
PATHS = ("/v1/push", "/v1/pull", "/health")


class CompactionScheduler:
    """Runs `compact()` every `interval_ms` on a daemon thread (the TypeScript `serve` timer)."""

    def __init__(self, server: AccordServer) -> None:
        self.server = server
        self.interval_s = server.definition.compaction.interval_ms / 1000
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self.interval_s <= 0 or self._thread is not None:
            return
        self._thread = threading.Thread(target=self._run, name="accord-compaction", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=30)
            self._thread = None

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            try:
                r = compact(self.server.pool, self.server.definition)
                log.info(
                    "compaction: %d records, %d ops folded below position %d, %d tombstones pruned",
                    r["records"],
                    r["opsFolded"],
                    r["watermark"],
                    r["tombstonesPruned"],
                )
            except Exception:
                log.exception("compaction failed")


def accord_router(
    server_or_definition: AccordServer | ServerDefinition,
    *,
    database_url: str | None = None,
    prefix: str = "",
    pool_size: int = 20,
    schedule_compaction: bool = True,
) -> APIRouter:
    """An `APIRouter` with `POST /v1/push`, `GET /v1/pull` and `GET /health` under `prefix`.

    Given a `ServerDefinition`, the router owns a connection pool on `database_url`: it opens it
    in the application's lifespan (or on the first request) and closes it at shutdown. Given an
    `AccordServer`, the caller owns its pool. With `schedule_compaction` and a definition whose
    `compaction.interval_ms` is above 0, the lifespan also compacts on that interval.
    """
    owns_pool = isinstance(server_or_definition, ServerDefinition)
    if isinstance(server_or_definition, ServerDefinition):
        if not database_url:
            raise ValueError("accord_router(definition) needs database_url=...")
        server = AccordServer(
            server_or_definition, create_pool(database_url, size=pool_size, open=False)
        )
    else:
        server = server_or_definition
    scheduler = CompactionScheduler(server)
    open_lock = threading.Lock()
    opened = [not owns_pool]

    def ensure_open() -> None:
        if opened[0]:
            return
        with open_lock:
            if not opened[0]:
                server.pool.open()
                opened[0] = True

    @asynccontextmanager
    async def lifespan(_app: Any) -> AsyncIterator[None]:
        ensure_open()
        if schedule_compaction:
            scheduler.start()
        try:
            yield
        finally:
            scheduler.stop()
            if owns_pool:
                server.pool.close()

    async def raw_body(request: Request) -> bytes:
        """The body, read at most one byte past the limit (a larger body gets 413 anyway)."""
        limit = server.max_body_bytes
        chunks: list[bytes] = []
        size = 0
        async for chunk in request.stream():
            chunks.append(chunk)
            size += len(chunk)
            if size > limit:
                break
        return b"".join(chunks)[: limit + 1]

    def make_endpoint(path: str) -> Callable[..., FastAPIResponse]:
        def endpoint(request: Request, body: bytes = Depends(raw_body)) -> FastAPIResponse:
            ensure_open()
            res = server.handle(
                request.method,
                path,
                request.url.query,
                dict(request.headers.items()),
                body,
            )
            out = FastAPIResponse(content=res.body, status_code=res.status)
            for name, value in res.headers:
                out.headers.append(name, value)
            return out

        endpoint.__name__ = "accord_" + path.strip("/").replace("/", "_")
        return endpoint

    router = APIRouter(prefix=prefix, lifespan=lifespan)
    for path in PATHS:
        router.add_api_route(
            path, make_endpoint(path), methods=METHODS, include_in_schema=path != "/health"
        )
    router.accord_server = server  # type: ignore[attr-defined]
    return router


def router_server(router: APIRouter) -> AccordServer:
    """The `AccordServer` behind a router made by `accord_router` (e.g. for an admin task)."""
    server = getattr(router, "accord_server", None)
    if not isinstance(server, AccordServer):
        raise TypeError("not a router made by accord_router()")
    return server
