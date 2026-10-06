"""Shared helpers: the server under test (`node/server.mjs`), its test-only routes, a lossy
network for the Python devices, and TypeScript devices driven over stdin/stdout."""

from __future__ import annotations

import json
import os
import random
import subprocess
from pathlib import Path
from typing import Any

import httpx
from accordsync import AccordClient, conflict, counter, define_schema, lww, set_
from accordsync_core import canonical_json

SERVER_URL = os.environ.get("ACCORD_URL", "http://localhost:8797")
TEST_URL = os.environ.get("ACCORD_TEST_URL", "http://localhost:8798")
NODE_DIR = Path(__file__).resolve().parent.parent / "node"

# The same schema as `node/schema.mjs`.
schema = define_schema(
    {
        "dossier": {
            "agent": lww(),
            "zone": lww(),
            "client_name": lww(),
            "status": conflict(),
            "visits": counter(),
            "docs": set_(),
        }
    }
)


def _test_route(path: str, params: httpx.QueryParams | None = None) -> dict[str, Any]:
    res = httpx.get(f"{TEST_URL}{path}", params=params, timeout=30)
    if res.status_code != 200:
        raise RuntimeError(f"{path} → {res.status_code} {res.text}")
    out: dict[str, Any] = res.json()
    return out


def require_server() -> None:
    """Fails with instructions when the server is not running."""
    try:
        httpx.get(f"{SERVER_URL}/health", timeout=5)
    except httpx.HTTPError as e:
        raise RuntimeError(
            f"No Accord server at {SERVER_URL}. Start it with interop/run.sh (see README)."
        ) from e


def reset_server() -> None:
    _test_route("/reset")


def compact_server() -> dict[str, Any]:
    return _test_route("/compact")


def token_for(sub: str, zones: list[str]) -> str:
    """A JWT for `sub` with read and write access to `zones`."""
    token: str = _test_route(
        "/token", httpx.QueryParams([("sub", sub), *(("zone", z) for z in zones)])
    )["token"]
    return token


class FlakyTransport(httpx.BaseTransport):
    """A network that loses requests, and loses responses after the server applied the request."""

    def __init__(self, rng: random.Random, loss: float) -> None:
        self.rng = rng
        self.loss = loss
        self._inner = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if self.rng.random() < self.loss / 2:
            raise httpx.ConnectError("network down (request lost)", request=request)
        res = self._inner.handle_request(request)
        res.read()
        if self.rng.random() < self.loss / 2:
            res.close()
            raise httpx.ReadError("network down (response lost)", request=request)
        return res

    def close(self) -> None:
        self._inner.close()


def snapshot_of(c: AccordClient) -> str:
    """The canonical state of every record a device holds, like the TypeScript side computes it."""
    return canonical_json({r: c.read(r) for r in c.records()})


class TsDevice:
    """A TypeScript device (`node/ts-client.mjs`), driven over stdin/stdout, one JSON line each."""

    def __init__(self, *, device_id: str, token: str, seed: int = 1, loss: float = 0) -> None:
        self._proc = subprocess.Popen(
            ["node", "ts-client.mjs"],  # noqa: S607
            cwd=NODE_DIR,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            text=True,
            encoding="utf-8",
        )
        self.call(
            {
                "cmd": "open",
                "deviceId": device_id,
                "token": token,
                "url": SERVER_URL,
                "seed": seed,
                "loss": loss,
            }
        )

    def call(self, command: dict[str, Any]) -> dict[str, Any]:
        assert self._proc.stdin is not None
        assert self._proc.stdout is not None
        self._proc.stdin.write(json.dumps(command) + "\n")
        self._proc.stdin.flush()
        line = self._proc.stdout.readline()
        if not line:
            raise RuntimeError(f"ts-client exited (code {self._proc.poll()})")
        answer: dict[str, Any] = json.loads(line)
        if answer.get("ok") is not True and command["cmd"] != "sync":
            raise RuntimeError(f"ts: {answer.get('error')}")
        return answer

    def snapshot(self) -> str:
        snap: str = self.call({"cmd": "snapshot"})["snapshot"]
        return snap

    def close(self) -> None:
        if self._proc.poll() is None:
            try:
                self.call({"cmd": "close"})
                self._proc.wait(timeout=10)
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                self._proc.kill()
                self._proc.wait()
