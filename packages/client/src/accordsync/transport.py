"""How a client reaches the server: the `Transport` protocol and the HTTP implementation."""

from __future__ import annotations

import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any, Protocol, TypeAlias

import httpx
from accordsync_core import OpId, RecordSnapshot, WireOp


@dataclass(frozen=True, slots=True)
class RefusedOp:
    op_id: OpId
    reason: str


@dataclass(frozen=True, slots=True)
class PushResult:
    """The server's answer to a push."""

    acked: Sequence[OpId]
    refused: Sequence[RefusedOp]

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> PushResult:
        return PushResult(
            acked=[str(i) for i in data["acked"]],
            refused=[RefusedOp(str(r["op_id"]), str(r["reason"])) for r in data["refused"]],
        )


@dataclass(frozen=True, slots=True)
class OpItem:
    """An op, in the wire format."""

    op: WireOp


@dataclass(frozen=True, slots=True)
class SnapshotItem:
    """A compacted record (ADR-0008)."""

    snapshot: RecordSnapshot


@dataclass(frozen=True, slots=True)
class ExitItem:
    """The record left this device's read scope (ADR-0004)."""

    record: str


PullItem: TypeAlias = OpItem | SnapshotItem | ExitItem


@dataclass(frozen=True, slots=True)
class PullPage:
    items: Sequence[PullItem]
    cursor: int
    has_more: bool
    device_seq: int | None = None
    """The highest op number the server has applied from this device."""


@dataclass(frozen=True, slots=True)
class ResyncRequired:
    """The server asks the device to drop its data and pull from zero."""


PullResult: TypeAlias = PullPage | ResyncRequired


def pull_item_from_json(data: Mapping[str, Any]) -> PullItem:
    t = data.get("type")
    if t == "op":
        return OpItem(dict(data["op"]))
    if t == "snapshot":
        return SnapshotItem(RecordSnapshot.from_json(data["snapshot"]))
    if t == "exit":
        return ExitItem(str(data["record"]))
    raise ValueError(f"unknown pull item type {t!r}")


def pull_result_from_json(data: Mapping[str, Any]) -> PullResult:
    if data.get("resync_required") is True:
        return ResyncRequired()
    seq = data.get("device_seq")
    return PullPage(
        items=[pull_item_from_json(i) for i in data["items"]],
        cursor=int(data["cursor"]),
        has_more=bool(data["has_more"]),
        device_seq=int(seq) if seq is not None else None,
    )


class Transport(Protocol):
    """How a client reaches the server. `HttpTransport` is the real one; tests can fake it."""

    def push(self, device_id: str, ops: Sequence[WireOp]) -> PushResult: ...

    def pull(self, device_id: str, cursor: int, limit: int) -> PullResult: ...


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


class HttpTransport:
    """Talks to an Accord server over HTTPS (see the server's docs/protocol.md).

    `url` is the server's base URL, e.g. `https://sync.example.com`. `get_token` returns the
    current JWT from your app's auth; it is called before every request.
    """

    def __init__(
        self,
        url: str,
        get_token: Callable[[], str],
        client: httpx.Client | None = None,
        timeout: float = 30.0,
    ) -> None:
        self._base = url.rstrip("/")
        self._get_token = get_token
        self._owned = client is None
        self._client = client if client is not None else httpx.Client(timeout=timeout)

    def push(self, device_id: str, ops: Sequence[WireOp]) -> PushResult:
        body = json.dumps({"ops": list(ops)}, separators=(",", ":"), allow_nan=False)
        return PushResult.from_json(self._call(device_id, "POST", "/v1/push", body))

    def pull(self, device_id: str, cursor: int, limit: int) -> PullResult:
        path = f"/v1/pull?cursor={cursor}&limit={limit}"
        return pull_result_from_json(self._call(device_id, "GET", path))

    def close(self) -> None:
        if self._owned:
            self._client.close()

    def _call(self, device_id: str, method: str, path: str, body: str | None = None) -> Any:
        headers = {
            "Authorization": f"Bearer {self._get_token()}",
            "Accord-Device": device_id,
        }
        if body is not None:
            headers["Content-Type"] = "application/json"
        res = self._client.request(
            method,
            f"{self._base}{path}",
            headers=headers,
            content=body.encode() if body is not None else None,
        )
        if not 200 <= res.status_code < 300:
            raise HttpError(res.status_code, f"{method} {path} → {res.status_code} {res.text}")
        return res.json()
