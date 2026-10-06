"""The mixed-server fleet's machinery: one PostgreSQL database, the TypeScript reference server
(Accord workspace sources) and the Python server running on it at once, devices of both languages
whose every request goes to a server picked at random through a lossy network."""

from __future__ import annotations

import json
import os
import random
import subprocess
import sys
import threading
import time
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit, urlunsplit

import httpx
import psycopg
from accordsync import AccordClient, HttpTransport, MemoryStorage
from accordsync_core import (
    RecordSnapshot,
    Replica,
    canonical_json,
    conflict,
    counter,
    decode_op,
    define_schema,
    lww,
    set_,
)
from accordsync_server import migrate

HERE = Path(__file__).resolve().parent.parent
PYTHON_DIR = HERE.parent
NODE_DIR = HERE / "node"
LOG_DIR = Path(os.environ.get("ACCORD_FLEET_LOGS", HERE / ".logs"))

ADMIN_URL = os.environ.get("ACCORD_DATABASE_URL", "")
APP_DIR = Path(os.environ.get("ACCORD_APP_DIR", PYTHON_DIR.parent / "app")).resolve()
TS_PORT = int(os.environ.get("ACCORD_TS_PORT", "8851"))
TS_CONTROL_PORT = int(os.environ.get("ACCORD_TS_CONTROL_PORT", "8852"))
PY_PORT = int(os.environ.get("ACCORD_PY_PORT", "8853"))
PY_CONTROL_PORT = int(os.environ.get("ACCORD_PY_CONTROL_PORT", "8854"))
DATABASE = "accord_fleet"
DEBUG = bool(os.environ.get("ACCORD_FLEET_DEBUG"))

SERVERS = {"ts": f"http://127.0.0.1:{TS_PORT}", "py": f"http://127.0.0.1:{PY_PORT}"}
CONTROL = {"ts": f"http://127.0.0.1:{TS_CONTROL_PORT}", "py": f"http://127.0.0.1:{PY_CONTROL_PORT}"}
TSX = ["node", "--import", "tsx", "--conditions=@accordsync/source"]

# The conformance profile's schema and scope function (app/conformance/PROFILE.md).
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


def scope_keys(fields: dict[str, object]) -> list[str]:
    keys = [f"{k}:{fields[k]}" for k in ("agent", "zone") if isinstance(fields.get(k), str)]
    return sorted(set(keys))


# ----------------------------------------------------------------------------- database


def database_url(name: str = DATABASE) -> str:
    """A URL (the TypeScript tools take URLs only) for database `name` on the admin server."""
    u = urlsplit(ADMIN_URL)
    return urlunsplit((u.scheme, u.netloc, f"/{name}", u.query, u.fragment))


def recreate_database(name: str = DATABASE) -> str:
    with psycopg.connect(ADMIN_URL, autocommit=True) as conn:
        conn.execute(f'drop database if exists "{name}" with (force)')
        conn.execute(f'create database "{name}"')
    return database_url(name)


def ledger(url: str) -> list[tuple[str, str]]:
    with psycopg.connect(url) as conn:
        return [
            (str(r[0]), str(r[1]))
            for r in conn.execute("select name, timestamp from kysely_migration order by name")
        ]


def migrate_ts(url: str) -> None:
    subprocess.run(  # noqa: S603
        [*TSX, str(NODE_DIR / "migrate.mts"), url],
        cwd=APP_DIR / "conformance",
        env={**os.environ, "ACCORD_APP_DIR": str(APP_DIR)},
        check=True,
        capture_output=True,
        timeout=120,
    )


def migrate_py(url: str) -> list[str]:
    return migrate(url)


def query(url: str, sql: str, params: tuple[object, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(url) as conn:
        return list(conn.execute(sql, params).fetchall())


def server_truth(url: str) -> tuple[dict[str, Any], list[str]]:
    """What the database holds: each record's state rebuilt from the feed, and every record whose
    `records` row (state or scopes) disagrees with its feed."""
    rebuilt: dict[str, Any] = {}
    wrong: list[str] = []
    with psycopg.connect(url) as conn:
        rows = conn.execute("select record, scopes, state from records order by record").fetchall()
        for record, scopes, state in rows:
            replica = Replica(schema)
            for kind, op in conn.execute(
                "select kind, op from feed where record = %s and kind in ('op', 'snapshot')"
                " order by pos, seq",
                (record,),
            ).fetchall():
                if kind == "snapshot":
                    replica.load_snapshot(RecordSnapshot.from_json(op))
                else:
                    replica.apply(decode_op(op))
            fields = replica.read(record) or {}
            rebuilt[record] = fields
            stored = Replica(schema)
            if state is not None:
                stored.load_snapshot(RecordSnapshot.from_json(state))
            if canonical_json(stored.read(record) or {}) != canonical_json(fields) or sorted(
                scopes
            ) != scope_keys(dict(fields)):
                wrong.append(record)
    return rebuilt, wrong


# ----------------------------------------------------------------------------- servers


def _wait_healthy(url: str, proc: subprocess.Popen[bytes], log: Path) -> None:
    for _ in range(240):
        if proc.poll() is not None:
            raise RuntimeError(f"server exited ({proc.returncode}):\n{log.read_text()}")
        try:
            if httpx.get(f"{url}/health", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.25)
    raise RuntimeError(f"server at {url} did not start:\n{log.read_text()}")


def _stop(proc: subprocess.Popen[bytes]) -> None:
    if proc.poll() is None:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


@contextmanager
def both_servers(url: str, tag: str) -> Iterator[None]:
    """The TypeScript reference server and the Python server, both on database `url`."""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    env = {**os.environ, "ACCORD_DATABASE_URL": url}
    procs: list[subprocess.Popen[bytes]] = []
    try:
        for name, cmd, cwd, port, control in (
            (
                "ts",
                [*TSX, "reference-server.ts"],
                APP_DIR / "conformance",
                TS_PORT,
                TS_CONTROL_PORT,
            ),
            (
                "py",
                # SIGUSR1 dumps every thread's stack into the log (for a hung request).
                [
                    sys.executable,
                    "-c",
                    "import faulthandler, runpy, signal;"
                    " faulthandler.register(signal.SIGUSR1, all_threads=True);"
                    " runpy.run_path('tools/conformance_server.py', run_name='__main__')",
                ],
                PYTHON_DIR,
                PY_PORT,
                PY_CONTROL_PORT,
            ),
        ):
            log = LOG_DIR / f"{tag}-{name}.log"
            with log.open("wb") as out:
                proc = subprocess.Popen(  # noqa: S603
                    cmd,
                    cwd=cwd,
                    env={**env, "ACCORD_PORT": str(port), "ACCORD_CONTROL_PORT": str(control)},
                    stdout=out,
                    stderr=subprocess.STDOUT,
                )
            procs.append(proc)
            # run.sh kills these by PID if pytest itself is killed.
            with (LOG_DIR / "servers.pid").open("a") as pids:
                pids.write(f"{proc.pid}\n")
            _wait_healthy(SERVERS[name], proc, log)
        yield
    finally:
        for proc in procs:
            _stop(proc)


def control(server: str, method: str, path: str, params: Any = None) -> dict[str, Any]:
    res = httpx.request(method, f"{CONTROL[server]}{path}", params=params, timeout=60)
    if res.status_code != 200:
        raise RuntimeError(f"{server} {path} → {res.status_code} {res.text}")
    out: dict[str, Any] = res.json()
    return out


def token_for(server: str, sub: str, zones: list[str]) -> str:
    token: str = control(
        server, "GET", "/token", httpx.QueryParams([("sub", sub), *(("zone", z) for z in zones)])
    )["token"]
    return token


# ----------------------------------------------------------------------------- devices


@dataclass
class Served:
    """Requests that reached each server, by server and kind (`push`, `pull`)."""

    counts: Counter[tuple[str, str]] = field(default_factory=Counter)
    lock: threading.Lock = field(default_factory=threading.Lock)

    def add(self, server: str, kind: str, n: int = 1) -> None:
        with self.lock:
            self.counts[(server, kind)] += n


class FleetTransport(httpx.BaseTransport):
    """Sends each request to a server picked at random; loses requests, and loses responses
    after the server applied the request."""

    def __init__(self, rng: random.Random, loss: float, served: Served) -> None:
        self.rng = rng
        self.loss = loss
        self.served = served
        self.log: list[tuple[str, str, str, str]] = []
        self._inner = httpx.HTTPTransport()

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        name = self.rng.choice(sorted(SERVERS))
        target = httpx.URL(SERVERS[name])
        request.url = request.url.copy_with(host=target.host, port=target.port)
        if self.rng.random() < self.loss / 2:
            raise httpx.ConnectError("network down (request lost)", request=request)
        self.served.add(name, request.url.path.rsplit("/", 1)[-1])
        res = self._inner.handle_request(request)
        res.read()
        if DEBUG:
            self.log.append((name, str(request.url), request.content.decode(), res.text))
        if self.rng.random() < self.loss / 2:
            res.close()
            raise httpx.ReadError("network down (response lost)", request=request)
        return res

    def close(self) -> None:
        self._inner.close()


class PyDevice:
    def __init__(self, device_id: str, token: str, rng: random.Random, served: Served) -> None:
        self.device_id = device_id
        self.token = token
        self.net = FleetTransport(rng, 0, served)
        self.client = AccordClient.open(
            schema=schema,
            storage=MemoryStorage(),
            device_id=device_id,
            transport=HttpTransport(
                "http://fleet",
                lambda: self.token,
                client=httpx.Client(transport=self.net, timeout=60),
            ),
        )

    def edit(self, cmd: str, record: str, field: str, value: Any) -> None:
        getattr(self.client, cmd)(record, field, value)

    def set_token(self, token: str) -> None:
        self.token = token

    def sync(self, lossy: bool) -> None:
        try:
            self.client.sync()
        except httpx.TransportError:
            if not lossy:
                raise

    def set_loss(self, loss: float) -> None:
        self.net.loss = loss

    def has(self, record: str) -> bool:
        return record in self.client.records()

    def snapshot(self) -> tuple[str, int]:
        c = self.client
        return canonical_json({r: c.read(r) for r in c.records()}), c.status().pending

    def close(self) -> None:
        self.client.close()


class TsDevice:
    """`node/ts-client.mts`: @accordsync/client from the workspace, over stdin/stdout."""

    def __init__(self, device_id: str, token: str, seed: int) -> None:
        self.device_id = device_id
        self._proc = subprocess.Popen(  # noqa: S603
            [*TSX, str(NODE_DIR / "ts-client.mts")],
            cwd=APP_DIR / "conformance",
            env={**os.environ, "ACCORD_APP_DIR": str(APP_DIR)},
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
                "servers": SERVERS,
                "seed": seed,
                "loss": 0,
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

    def set_token(self, token: str) -> None:
        self.call({"cmd": "token", "token": token})

    def edit(self, cmd: str, record: str, field: str, value: Any) -> None:
        self.call({"cmd": cmd, "record": record, "field": field, "value": value})

    def sync(self, lossy: bool) -> None:
        answer = self.call({"cmd": "sync"})
        if answer["ok"] is not True and not (lossy and "network down" in answer["error"]):
            raise RuntimeError(f"{self.device_id} sync: {answer['error']}")

    def set_loss(self, loss: float) -> None:
        self.call({"cmd": "loss", "loss": loss})

    def has(self, record: str) -> bool:
        return bool(self.call({"cmd": "has", "record": record})["has"])

    def snapshot(self) -> tuple[str, int]:
        answer = self.call({"cmd": "snapshot"})
        return str(answer["snapshot"]), int(answer["pending"])

    def served(self) -> dict[str, dict[str, int]]:
        served: dict[str, dict[str, int]] = self.call({"cmd": "snapshot"})["served"]
        return served

    def close(self) -> None:
        if self._proc.poll() is None:
            try:
                self.call({"cmd": "close"})
                self._proc.wait(timeout=10)
            except (RuntimeError, OSError, subprocess.TimeoutExpired):
                self._proc.kill()
                self._proc.wait()


Device = PyDevice | TsDevice
