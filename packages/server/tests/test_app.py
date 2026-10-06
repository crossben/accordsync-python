"""The request layer end to end on PostgreSQL. Needs ACCORD_TEST_DATABASE_URL.

The conformance suite (tools/conformance_server.py) covers the protocol; these check what it
cannot reach: CORS, the CLI, and the handler's plumbing.
"""

from __future__ import annotations

import json
import time
from collections.abc import Iterator

import jwt
import pytest
from accordsync_core import counter, define_schema, lww
from accordsync_server import (
    Access,
    AccordServer,
    Auth,
    Compaction,
    ScopedRecord,
    compact,
    create_pool,
    define_server,
    migrate,
)
from accordsync_server.__main__ import main
from accordsync_server.sync import Pool

SECRET = "accord-test-secret-at-least-32-bytes-long"  # noqa: S105 (a test key)


def scopes(r: ScopedRecord) -> list[str]:
    return [f"agent:{r.fields['agent']}"] if isinstance(r.fields.get("agent"), str) else []


def access(claims: dict[str, object]) -> Access:
    return Access(read=[f"agent:{claims['sub']}"], write=[f"agent:{claims['sub']}"])


definition = define_server(
    schema=define_schema({"dossier": {"agent": lww(), "visits": counter()}}),
    scopes={"dossier": scopes},
    access=access,
    auth=Auth.hs256(SECRET),
    cors=["https://app.example.com"],
    compaction=Compaction(min_ops=2),
)


@pytest.fixture
def pool(database_url: str) -> Iterator[Pool]:
    migrate(database_url)
    with create_pool(database_url, size=4) as p:
        yield p


def headers(sub: str = "alice", device: str = "phone") -> dict[str, str]:
    token = jwt.encode({"sub": sub, "exp": int(time.time()) + 60}, SECRET, algorithm="HS256")
    return {"Authorization": f"Bearer {token}", "Accord-Device": device}


def ops(device: str = "phone") -> list[dict[str, object]]:
    now = int(time.time() * 1000)
    return [
        {"op_id": f"{device}:1", "record": "dossier:1", "field": "agent",
         "hlc": f"{now}:00000:{device}", "kind": "assign", "value": "alice", "deps": []},
        {"op_id": f"{device}:2", "record": "dossier:1", "field": "visits",
         "hlc": f"{now}:00001:{device}", "kind": "inc", "by": 2},
    ]  # fmt: skip


def test_push_then_pull_through_handle(pool: Pool) -> None:
    server = AccordServer(definition, pool)
    body = json.dumps({"ops": ops()}).encode()
    res = server.handle("POST", "/v1/push", "", headers(), body)
    assert res.status == 200
    assert json.loads(res.body) == {"acked": ["phone:1", "phone:2"], "refused": []}
    assert ("Accord-Protocol", "1") in res.headers
    res = server.handle("GET", "/v1/pull", "cursor=0", headers())
    page = json.loads(res.body)
    assert [i["op"]["op_id"] for i in page["items"]] == ["phone:1", "phone:2"]
    assert page["device_seq"] == 2
    assert page["has_more"] is False


def test_errors_carry_the_protocol_header(pool: Pool) -> None:
    server = AccordServer(definition, pool)
    for status, args in [
        (401, ("GET", "/v1/pull", "cursor=0", {"Accord-Device": "phone"})),
        (400, ("GET", "/v1/pull", "cursor=x", headers())),
        (404, ("GET", "/nope", "", {})),
        (413, ("POST", "/v1/push", "", headers(), b"x" * (5 * 1024 * 1024 + 1))),
    ]:
        res = server.handle(*args)  # type: ignore[arg-type]
        assert res.status == status
        assert ("Accord-Protocol", "1") in res.headers
        assert set(json.loads(res.body)) == {"error"}


def test_cors_preflight_and_simple_requests(pool: Pool) -> None:
    server = AccordServer(definition, pool)
    origin = {"Origin": "https://app.example.com"}
    pre = server.handle("OPTIONS", "/v1/push", "", origin)
    assert pre.status == 204
    h = dict(pre.headers)
    assert h["Access-Control-Allow-Origin"] == "https://app.example.com"
    assert "Accord-Device" in h["Access-Control-Allow-Headers"]
    res = server.handle("GET", "/health", "", origin)
    assert dict(res.headers)["Access-Control-Expose-Headers"] == "Accord-Protocol"
    other = server.handle("GET", "/health", "", {"Origin": "https://evil.example.com"})
    assert "Access-Control-Allow-Origin" not in dict(other.headers)


def test_wsgi_adapter(pool: Pool) -> None:
    import io

    server = AccordServer(definition, pool)
    seen: list[str] = []
    out = server.wsgi(
        {"REQUEST_METHOD": "GET", "PATH_INFO": "/health", "QUERY_STRING": "",
         "wsgi.input": io.BytesIO()},
        lambda status, headers: seen.append(status),
    )  # fmt: skip
    assert seen == ["200 OK"]
    assert json.loads(b"".join(out)) == {"status": "ok", "protocolVersion": 1}


def test_compaction_function_and_cli(
    pool: Pool, database_url: str, capsys: pytest.CaptureFixture[str]
) -> None:
    server = AccordServer(definition, pool)
    server.handle("POST", "/v1/push", "", headers(), json.dumps({"ops": ops()}).encode())
    # Compaction waits for every live device: this one has to pull past its ops first.
    assert compact(pool, definition)["records"] == 0
    cursor = json.loads(server.handle("GET", "/v1/pull", "cursor=0", headers()).body)["cursor"]
    server.handle("GET", "/v1/pull", f"cursor={cursor}", headers())
    result = compact(pool, definition)
    assert result["records"] == 1
    assert result["opsFolded"] == 2
    assert set(result) == {"watermark", "records", "opsFolded", "tombstonesPruned"}
    assert main(["migrate", "--database-url", database_url]) == 0
    assert "up to date" in capsys.readouterr().out
    assert main(["compact", "--database-url", database_url,
                 "--definition", f"{__name__}:definition"]) == 0  # fmt: skip
    assert json.loads(capsys.readouterr().out)["records"] == 0
