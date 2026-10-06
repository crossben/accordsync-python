"""Wiring tests: the router passes requests through to `AccordServer.handle()` unchanged."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import pytest
from accordsync_core import define_schema, lww
from accordsync_fastapi import PROTOCOL_VERSION, accord_router, router_server
from accordsync_server import (
    Access,
    AccordServer,
    Auth,
    Limits,
    Response,
    ServerDefinition,
    define_server,
)
from fastapi import FastAPI
from fastapi.testclient import TestClient

DEFINITION = define_server(
    schema=define_schema({"note": {"title": lww()}}),
    scopes={"note": lambda r: ["all"]},
    access=lambda c: Access(read=["all"], write=["all"]),
    auth=Auth.hs256("x" * 32),
    cors=["https://app.example"],
    limits=Limits(max_body_bytes=100),
)


class DeadPool:
    """A pool whose connections always fail: no database in these tests."""

    def connection(self, **_: Any) -> Any:
        raise RuntimeError("no database")


class StubServer(AccordServer):
    def __init__(self, definition: ServerDefinition) -> None:
        super().__init__(definition, DeadPool())  # type: ignore[arg-type]
        self.calls: list[tuple[str, str, str, dict[str, str], bytes]] = []

    def handle(
        self,
        method: str,
        path: str,
        query: Any,
        headers: Mapping[str, str],
        body: bytes = b"",
    ) -> Response:
        self.calls.append((method, path, query, dict(headers), body))
        return Response(
            418, [("Content-Type", "text/plain"), ("Retry-After", "3"), ("Vary", "A")], b"stub"
        )


def client(server: AccordServer, prefix: str = "") -> TestClient:
    app = FastAPI()
    app.include_router(accord_router(server, prefix=prefix))
    return TestClient(app)


def test_speaks_protocol_version_1() -> None:
    assert PROTOCOL_VERSION == 1


def test_passes_raw_request_and_response_through() -> None:
    stub = StubServer(DEFINITION)
    res = client(stub, "/sync").post(
        "/sync/v1/push?a=1&a=2", content=b'{"ops": [1.0]}', headers={"Accord-Device": "d1"}
    )
    assert res.status_code == 418
    assert res.content == b"stub"
    assert res.headers["retry-after"] == "3"
    assert res.headers["vary"] == "A"
    method, path, query, headers, body = stub.calls[0]
    assert (method, path, query, body) == ("POST", "/v1/push", "a=1&a=2", b'{"ops": [1.0]}')
    assert headers["accord-device"] == "d1"


@pytest.mark.parametrize("method", ["GET", "HEAD", "PUT", "DELETE", "OPTIONS"])
def test_every_method_reaches_handle(method: str) -> None:
    stub = StubServer(DEFINITION)
    res = client(stub).request(method, "/v1/push")
    assert res.status_code == 418
    assert stub.calls[0][:2] == (method, "/v1/push")


def test_body_is_cut_one_byte_past_the_limit() -> None:
    stub = StubServer(DEFINITION)
    client(stub).post("/v1/push", content=b"x" * 10_000)
    assert stub.calls[0][4] == b"x" * 101


def test_real_handle_without_database() -> None:
    c = client(AccordServer(DEFINITION, DeadPool()))  # type: ignore[arg-type]
    health = c.get("/health")
    assert health.status_code == 503
    assert health.headers["accord-protocol"] == "1"
    assert c.post("/v1/push", content=b"{}").status_code == 401
    assert c.post("/v1/push", content=b"x" * 101).status_code == 413
    pre = c.options(
        "/v1/pull",
        headers={"Origin": "https://app.example", "Access-Control-Request-Method": "GET"},
    )
    assert pre.status_code == 204
    assert pre.headers["access-control-allow-origin"] == "https://app.example"
    assert c.get("/v1/push").json() == {"error": "not found"}


def test_definition_needs_database_url() -> None:
    with pytest.raises(ValueError, match="database_url"):
        accord_router(DEFINITION)


def test_router_server() -> None:
    server = AccordServer(DEFINITION, DeadPool())  # type: ignore[arg-type]
    assert router_server(accord_router(server)) is server
    with pytest.raises(TypeError):
        router_server(FastAPI().router)


def test_owned_pool_against_postgres() -> None:
    url = os.environ.get("ACCORD_TEST_DATABASE_URL")
    if not url:
        pytest.skip("ACCORD_TEST_DATABASE_URL is not set")
    app = FastAPI()
    app.include_router(accord_router(DEFINITION, database_url=url))
    with TestClient(app) as c:
        assert c.get("/health").json() == {"protocolVersion": 1, "status": "ok"}
