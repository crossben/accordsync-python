"""The mixed fleet: Python devices and TypeScript devices work on the same records through the real
server over a network that loses requests and responses, with a compaction mid-run. After the
network heals, every device must hold byte-identical canonical snapshots.
Port of flutter/interop/test/mixed_fleet_test.dart."""

from __future__ import annotations

import contextlib
import os
import random
from typing import Any

import httpx
import pytest
from accordsync import AccordClient, HttpTransport, MemoryStorage
from support import (
    SERVER_URL,
    FlakyTransport,
    TsDevice,
    compact_server,
    require_server,
    reset_server,
    schema,
    snapshot_of,
    token_for,
)

SEEDS = [int(s) for s in os.environ.get("ACCORD_INTEROP_SEEDS", "1,2,3").split(",") if s.strip()]
LOSS = 0.25
RECORDS = ["dossier:1", "dossier:2", "dossier:é"]


@pytest.fixture(scope="module", autouse=True)
def _server() -> None:
    require_server()


def _edit(rng: random.Random) -> tuple[str, str, Any]:
    match rng.randrange(7):
        case 0:
            return ("inc", "visits", rng.randrange(9) - 3)
        case 1 | 2 as k:
            cmd = "add" if k == 1 else "remove"
            element = f"doc-{rng.randrange(4)}" if rng.random() < 0.5 else rng.randrange(3)
            return (cmd, "docs", element)
        case 3:
            return ("assign", "status", ["s0", "s1", None, 2.5, "é"][rng.randrange(5)])
        case 4:
            return ("assign", "client_name", {"n": rng.randrange(9), "10": True, "a": 1e21})
        case _:
            return ("sync", "", None)


@pytest.mark.parametrize("seed", SEEDS)
def test_python_and_typescript_devices_converge(seed: int) -> None:
    reset_server()
    rng = random.Random(seed)  # noqa: S311
    awa = token_for("awa", ["dakar"])
    moussa = token_for("moussa", ["dakar"])

    networks: list[FlakyTransport] = []
    py: list[AccordClient] = []
    ts: list[TsDevice] = []

    def python_device(device_id: str, token: str) -> AccordClient:
        net = FlakyTransport(random.Random(seed * 31 + len(networks)), LOSS)  # noqa: S311
        networks.append(net)
        return AccordClient.open(
            schema=schema,
            storage=MemoryStorage(),
            device_id=device_id,
            transport=HttpTransport(
                SERVER_URL, lambda: token, client=httpx.Client(transport=net, timeout=30)
            ),
        )

    def settle() -> None:
        for n in networks:
            n.loss = 0
        for t in ts:
            t.call({"cmd": "heal"})
        for _ in range(3):
            for d in py:
                d.sync()
            for t in ts:
                answer = t.call({"cmd": "sync"})
                assert answer["ok"] is True, answer

    try:
        py += [python_device("py-awa", awa), python_device("py-moussa", moussa)]
        ts.append(TsDevice(device_id="ts-awa", token=awa, seed=seed * 7 + 1, loss=LOSS))
        ts.append(TsDevice(device_id="ts-moussa", token=moussa, seed=seed * 7 + 2, loss=LOSS))

        # Every record starts in the shared zone, written by both kinds of device.
        py[0].assign(RECORDS[0], "zone", "dakar")
        ts[0].call({"cmd": "assign", "record": RECORDS[1], "field": "zone", "value": "dakar"})
        py[1].assign(RECORDS[2], "zone", "dakar")

        compactions = 0
        for step in range(150):
            record = RECORDS[rng.randrange(len(RECORDS))]
            cmd, field, value = _edit(rng)
            i = rng.randrange(4)
            if i < 2:
                d = py[i]
                if cmd == "inc":
                    d.inc(record, field, value)
                elif cmd == "add":
                    d.add(record, field, value)
                elif cmd == "remove":
                    d.remove(record, field, value)
                elif cmd == "assign":
                    d.assign(record, field, value)
                else:
                    # The lossy network: the outbox is retried next round.
                    with contextlib.suppress(httpx.TransportError):
                        d.sync()
            else:
                ts[i - 2].call(
                    {"cmd": "sync"}
                    if cmd == "sync"
                    else {"cmd": cmd, "record": record, "field": field, "value": value}
                )
            if step == 75:
                # Compaction folds only what every live device has pulled (its watermark is the
                # lowest device cursor), so settle the fleet first, then go back to the lossy net.
                settle()
                compactions = int(compact_server()["records"])
                assert compactions > 0
                for n in networks:
                    n.loss = LOSS
                for t in ts:
                    t.call({"cmd": "loss", "loss": LOSS})

        # Heal the network, then sync everyone until nothing is pending and all are current.
        settle()

        snapshots = {d.device_id: snapshot_of(d) for d in py}
        snapshots.update({f"ts-{i}": t.snapshot() for i, t in enumerate(ts)})
        assert len(set(snapshots.values())) == 1, (
            f"seed {seed} ({compactions} records compacted mid-run): {snapshots}"
        )
        assert "dossier:é" in next(iter(snapshots.values()))
        for d in py:
            assert d.status().pending == 0
        for t in ts:
            assert t.call({"cmd": "snapshot"})["pending"] == 0
    finally:
        for t in ts:
            t.close()
        for d in py:
            d.close()
