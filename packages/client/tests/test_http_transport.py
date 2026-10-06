import json

import httpx
import pytest
from accordsync import (
    ExitItem,
    HttpError,
    HttpTransport,
    OpItem,
    PullPage,
    RefusedOp,
    ResyncRequired,
    SnapshotItem,
)

OP = {"op_id": "d:1", "record": "t:1", "field": "f", "kind": "inc", "by": 1, "hlc": "1:00000:d"}


def make(handler: httpx.MockTransport, token: str = "tok") -> HttpTransport:  # noqa: S107
    return HttpTransport(
        "https://sync.example.com//",
        get_token=lambda: token,
        client=httpx.Client(transport=handler),
    )


def test_push_sends_auth_device_and_json() -> None:
    seen: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(
            200, json={"acked": ["d:1"], "refused": [{"op_id": "d:2", "reason": "no"}]}
        )

    res = make(httpx.MockTransport(handle)).push("dev-1", [OP])
    assert list(res.acked) == ["d:1"]
    assert list(res.refused) == [RefusedOp("d:2", "no")]
    (req,) = seen
    assert req.method == "POST"
    assert str(req.url) == "https://sync.example.com/v1/push"
    assert req.headers["authorization"] == "Bearer tok"
    assert req.headers["accord-device"] == "dev-1"
    assert req.headers["content-type"] == "application/json"
    assert json.loads(req.content) == {"ops": [OP]}


def test_pull_parses_items() -> None:
    seen: list[httpx.Request] = []

    def handle(req: httpx.Request) -> httpx.Response:
        seen.append(req)
        return httpx.Response(
            200,
            json={
                "items": [
                    {"type": "op", "op": OP},
                    {
                        "type": "snapshot",
                        "snapshot": {
                            "record": "t:2",
                            "fields": {"f": {"strategy": "counter", "total": 3}},
                        },
                    },
                    {"type": "exit", "record": "t:3"},
                ],
                "cursor": 12,
                "has_more": True,
                "device_seq": 4,
            },
        )

    page = make(httpx.MockTransport(handle)).pull("dev-1", 5, 100)
    assert isinstance(page, PullPage)
    assert page.cursor == 12
    assert page.has_more is True
    assert page.device_seq == 4
    assert page.items[0] == OpItem(OP)
    assert isinstance(page.items[1], SnapshotItem)
    assert page.items[1].snapshot.record == "t:2"
    assert page.items[2] == ExitItem("t:3")
    (req,) = seen
    assert req.method == "GET"
    assert str(req.url) == "https://sync.example.com/v1/pull?cursor=5&limit=100"
    assert req.headers["authorization"] == "Bearer tok"
    assert req.headers["accord-device"] == "dev-1"


def test_pull_without_device_seq_and_resync() -> None:
    answers = [
        {"items": [], "cursor": 0, "has_more": False},
        {"resync_required": True},
    ]

    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=answers.pop(0))

    t = make(httpx.MockTransport(handle))
    page = t.pull("d", 0, 10)
    assert isinstance(page, PullPage)
    assert page.device_seq is None
    assert isinstance(t.pull("d", 0, 10), ResyncRequired)


def test_error_status_raises_http_error() -> None:
    def handle(_: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text="bad token")

    with pytest.raises(HttpError) as info:
        make(httpx.MockTransport(handle)).pull("d", 0, 10)
    assert info.value.status == 401
    assert "bad token" in info.value.message


def test_token_is_read_before_every_request() -> None:
    tokens = iter(["a", "b"])
    auth: list[str] = []

    def handle(req: httpx.Request) -> httpx.Response:
        auth.append(req.headers["authorization"])
        return httpx.Response(200, json={"items": [], "cursor": 0, "has_more": False})

    t = HttpTransport(
        "https://x",
        get_token=lambda: next(tokens),
        client=httpx.Client(transport=httpx.MockTransport(handle)),
    )
    t.pull("d", 0, 1)
    t.pull("d", 0, 1)
    assert auth == ["Bearer a", "Bearer b"]
