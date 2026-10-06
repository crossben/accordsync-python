"""The mixed-server fleet (plan-python.md §0 point 4): one PostgreSQL database, the TypeScript
reference server and the Python server running on it at once. Two Python and two TypeScript
devices send every request to a server picked at random, through a network that loses requests
and responses; seeded random edits with awkward values; a compaction and a scope change mid-run.
After the network heals, every device must hold byte-identical canonical snapshots, equal to what
the database holds, and both servers must have served pushes and pulls."""

from __future__ import annotations

import json
import os
import random
import time
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from typing import Any

import httpx
import pytest
from accordsync_core import LocalWriter, canonical_json, encode_op
from fleet import (
    ADMIN_URL,
    DEBUG,
    LOG_DIR,
    SERVERS,
    Device,
    PyDevice,
    Served,
    TsDevice,
    both_servers,
    control,
    ledger,
    migrate_py,
    migrate_ts,
    query,
    recreate_database,
    schema,
    server_truth,
    token_for,
)

SEEDS = [int(s) for s in os.environ.get("ACCORD_FLEET_SEEDS", "1,2,3").split(",") if s.strip()]
LOSS = 0.25
ROUNDS = 160
COMPACT_AT = 60
SCOPE_AT = 110
SHARED = ["dossier:1", "dossier:2", "dossier:é"]
THIES = "dossier:4"  # created by awa in zone thies; moussa gets thies mid-run
MIGRATIONS = 6

AWKWARD = [None, 2.5, 1e21, "é", "", "😀", "s0", 0, -1]
ELEMENTS = ["doc-0", "doc-1", 0, 1, 2.5, "", "😀", "é"]


@pytest.fixture(scope="module", autouse=True)
def _database() -> None:
    if not ADMIN_URL:
        pytest.fail("ACCORD_DATABASE_URL is not set: run server-interop/run.sh")


def _now() -> int:
    return int(time.time() * 1000)


def _migrate_once(url: str, seed: int) -> str:
    """Migrates with one implementation (even seeds TypeScript, odd Python); the other must
    find nothing to do."""
    first = "ts" if seed % 2 == 0 else "py"
    if first == "ts":
        migrate_ts(url)
    else:
        assert len(migrate_py(url)) == MIGRATIONS
    done = ledger(url)
    assert len(done) == MIGRATIONS, done
    if first == "ts":
        assert migrate_py(url) == []
    else:
        migrate_ts(url)
    assert ledger(url) == done, "the second implementation migrated again"
    return first


def _action(rng: random.Random, record: str) -> tuple[str, str, Any]:
    match rng.randrange(10):
        case 0:
            return ("inc", "visits", rng.randrange(9) - 3)
        case 1 | 2 as k:
            return ("add" if k == 1 else "remove", "docs", rng.choice(ELEMENTS))
        case 3:
            return ("assign", "status", rng.choice(AWKWARD))
        case 4:
            n = rng.randrange(9)
            value = rng.choice([{"10": True, "a": 1e21, "n": n}, "é", "", "😀", None, 2.5])
            return ("assign", "client_name", value)
        case 5:
            return ("assign", "agent", rng.choice(["awa", "moussa", None]))
        case 6 if record == THIES:
            return ("assign", "zone", rng.choice(["thies", "dakar"]))
        case _:
            return ("sync", "", None)


def _http(server: str, method: str, path: str, token: str, device: str, **kw: Any) -> Any:
    res = httpx.request(
        method,
        f"{SERVERS[server]}{path}",
        headers={"Authorization": f"Bearer {token}", "Accord-Device": device},
        timeout=60,
        **kw,
    )
    assert res.status_code == 200, f"{server} {path}: {res.status_code} {res.text}"
    return res.json()


def _push(server: str, token: str, device: str, ops: list[Any]) -> Any:
    return _http(server, "POST", "/v1/push", token, device, json={"ops": ops})


def _pull(server: str, token: str, device: str, cursor: int) -> Any:
    return _http(server, "GET", "/v1/pull", token, device, params={"cursor": cursor, "limit": 10})


def _pull_all(server: str, token: str, device: str, cursor: int) -> int:
    """Pulls to the end. The server records a device's cursor from its requests (what it has
    applied), so the last page is followed by one more pull at its cursor, which is returned."""
    while True:
        page = _pull(server, token, device, cursor)
        assert "resync_required" not in page, page
        cursor = page["cursor"]
        if not page["has_more"]:
            last = _pull(server, token, device, cursor)
            assert last["items"] == [], last
            return cursor


@pytest.mark.parametrize("seed", SEEDS)
def test_mixed_server_fleet(seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311
    url = recreate_database()
    migrated_by = _migrate_once(url, seed)
    served = Served()
    with both_servers(url, f"seed{seed}"):
        _fleet(seed, rng, url, served, migrated_by)


def _fleet(seed: int, rng: random.Random, url: str, served: Served, migrated_by: str) -> None:
    def server() -> str:
        return rng.choice(sorted(SERVERS))

    # A token from either control API works on both servers (same secret and issuer).
    awa = token_for("ts", "awa", ["dakar", "thies"])
    moussa = token_for("py", "moussa", ["dakar"])

    py = [
        PyDevice("py-awa", awa, random.Random(seed * 31 + 1), served),  # noqa: S311
        PyDevice("py-moussa", moussa, random.Random(seed * 31 + 2), served),  # noqa: S311
    ]
    ts: list[TsDevice] = []
    try:
        ts += [TsDevice("ts-awa", awa, seed * 7 + 1), TsDevice("ts-moussa", moussa, seed * 7 + 2)]
        devices: list[Device] = [py[0], ts[0], py[1], ts[1]]
        is_awa = {d.device_id: d.device_id.endswith("awa") for d in devices}

        def set_loss(loss: float) -> None:
            for d in devices:
                d.set_loss(loss)

        def settle() -> None:
            set_loss(0)
            for _ in range(3):
                for d in devices:
                    d.sync(lossy=False)

        # Every shared record starts in zone dakar, written by both kinds of device; awa's
        # TypeScript device creates the 4th in zone thies, which moussa cannot see yet.
        py[0].edit("assign", SHARED[0], "zone", "dakar")
        ts[0].edit("assign", SHARED[1], "zone", "dakar")
        py[1].edit("assign", SHARED[2], "zone", "dakar")
        ts[0].edit("assign", THIES, "zone", "thies")
        settle()
        set_loss(LOSS)

        def act(d: Device, then_sync: bool, record: str, cmd: str, field: str, value: Any) -> None:
            # Moussa edits the thies record only while he holds it (a racing move can still get
            # the op refused: the client must cope).
            if cmd != "sync" and (is_awa[d.device_id] or record != THIES or d.has(THIES)):
                d.edit(cmd, record, field, value)
                if not then_sync:
                    return
            d.sync(lossy=True)

        compacted: dict[str, Any] = {}
        with ThreadPoolExecutor(max_workers=len(devices)) as pool:
            for step in range(ROUNDS):
                plan = []
                for d in devices:
                    record = rng.choice([*SHARED, THIES])
                    plan.append((d, rng.random() < 0.8, record, *_action(rng, record)))
                # The four devices act at once: pushes and pulls overlap on both servers.
                for f in [pool.submit(act, *p) for p in plan]:
                    f.result()

                if step == COMPACT_AT:
                    compacted = _compact_mid_run(server, url, awa, settle)
                    set_loss(LOSS)
                if step == SCOPE_AT:
                    settle()
                    moussa = _scope_change(server, awa)
                    for d in devices:
                        if not is_awa[d.device_id]:
                            d.set_token(moussa)
                            # Known bug in both servers (see test_a_lost_scope_delta_is_sent_again):
                            # the scope delta is sent once; if that page is lost, never again. So
                            # the first pull with the new token goes over a healthy network.
                            d.sync(lossy=False)
                    set_loss(LOSS)

        # Heal the network, then sync everyone until nothing is pending and all are current.
        settle()

        snapshots = {d.device_id: d.snapshot() for d in devices}
        if DEBUG and len({s for s, _ in snapshots.values()}) > 1:
            LOG_DIR.mkdir(parents=True, exist_ok=True)
            for p in py:
                with (LOG_DIR / f"seed{seed}-{p.device_id}.http.jsonl").open("w") as out:
                    out.writelines(json.dumps(line) + "\n" for line in p.net.log)
                with (LOG_DIR / f"seed{seed}-{p.device_id}.ops.jsonl").open("w") as out:
                    out.writelines(
                        json.dumps(encode_op(o)) + "\n" for o in p.client._writer.replica.ops()
                    )
            with (LOG_DIR / f"seed{seed}-feed.jsonl").open("w") as out:
                feed = "select seq, pos, kind, record, op_id, op from feed order by pos, seq"
                out.writelines(json.dumps(row, default=str) + "\n" for row in query(url, feed))
        assert len({s for s, _ in snapshots.values()}) == 1, _divergence(seed, url, snapshots)
        assert all(p == 0 for _, p in snapshots.values()), snapshots
        snapshot = next(iter(snapshots.values()))[0]
        assert "dossier:é" in snapshot
        assert THIES in snapshot

        # What the database holds agrees with its feed, and with every device.
        rebuilt, wrong = server_truth(url)
        assert wrong == [], f"records rows disagree with the feed: {wrong}"
        visible = {r: v for r, v in rebuilt.items() if r in json.loads(snapshot)}
        assert canonical_json(visible) == snapshot

        # Both servers served pushes and pulls.
        counts: Counter[tuple[str, str]] = Counter(served.counts)
        for t in ts:
            for name, kinds in t.served().items():
                for kind, n in kinds.items():
                    counts[(name, kind)] += n
        summary = {
            "seed": seed,
            "migrated_by": migrated_by,
            "compacted": compacted,
            "served": {f"{s}/{k}": n for (s, k), n in sorted(counts.items())},
        }
        print(json.dumps(summary))
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with (LOG_DIR / "summary.jsonl").open("a", encoding="utf-8") as out:
            out.write(json.dumps(summary) + "\n")
        for name in SERVERS:
            for kind in ("push", "pull"):
                assert counts[(name, kind)] > 0, f"{name} served no {kind}: {counts}"
    finally:
        for t in ts:
            t.close()
        for p in py:
            p.close()


def _divergence(seed: int, url: str, snapshots: dict[str, tuple[str, int]]) -> str:
    """Which devices differ from the database, record by record and field by field."""
    rebuilt, wrong = server_truth(url)
    lines = [f"seed {seed}: devices diverge (records rows disagreeing with the feed: {wrong})"]
    for device, (snap, pending) in snapshots.items():
        held = json.loads(snap)
        for record in sorted(set(held) | set(rebuilt)):
            mine, truth = held.get(record), json.loads(canonical_json(rebuilt.get(record)))
            if mine == truth:
                continue
            fields = sorted(set(mine or {}) | set(truth or {}))
            diff = {
                f: ((mine or {}).get(f), (truth or {}).get(f))
                for f in fields
                if (mine or {}).get(f) != (truth or {}).get(f)
            }
            lines.append(f"  {device} (pending {pending}) {record}: (device, database) {diff}")
    return "\n".join(lines)


def _compact_mid_run(
    server: Callable[[], str], url: str, awa: str, settle: Callable[[], None]
) -> dict[str, Any]:
    """Heal, settle, compact through a random server's control API. A probe device pushes an op
    with awkward values first and pulls past it, as if its ack was lost; after the compaction
    folds it, its retry must be acked by both servers, and the same id with other content refused
    by both (each server checks the op_hash the other may have written)."""
    settle()  # the outbox ops of the lossy phase land before the probe op
    writer = LocalWriter(schema, "probe-awa", _now)
    op = encode_op(
        writer.assign(SHARED[0], "client_name", {"10": True, "a": 1e21, "n": "é😀", "z": [2.5]})
    )
    first = _push(server(), awa, "probe-awa", [op])
    assert first == {"acked": [op["op_id"]], "refused": []}, first
    _pull_all(server(), awa, "probe-awa", 0)

    # Compaction folds only what every live device has pulled (its watermark is the lowest
    # device cursor), so settle the fleet first.
    settle()
    by = server()
    result = control(by, "POST", "/compact")
    assert result["records"] > 0, result
    assert query(url, "select 1 from compacted_ops where op_id = %s", (op["op_id"],)), (
        "the probe op was not folded",
        result,
        query(url, "select device_id, cursor from devices order by cursor"),
        query(url, "select record, max(pos), count(*) from feed group by record"),
    )
    tampered = {**op, "value": "other"}
    for s in sorted(SERVERS):
        again = _push(s, awa, "probe-awa", [op])
        assert again == {"acked": [op["op_id"]], "refused": []}, (
            f"compacted by {by}, retried on {s}: {again}"
        )
        bad = _push(s, awa, "probe-awa", [tampered])
        assert bad["acked"] == [], f"compacted by {by}, tampered retry on {s}: {bad}"
        assert bad["refused"][0]["reason"].startswith("op id already used"), bad
    return {"by": by, **result}


def _pages(server: str, token: str, device: str, cursor: int) -> tuple[list[Any], int]:
    """Every item after `cursor`, page by page, and the cursor at the end."""
    items: list[Any] = []
    while True:
        page = _pull(server, token, device, cursor)
        assert "resync_required" not in page, page
        items += page["items"]
        cursor = page["cursor"]
        if not page["has_more"]:
            return items, cursor


def _has(items: list[Any], record: str) -> bool:
    """Whether `items` carry `record`'s history: a snapshot, or more than the one op that moved
    it (a record entering a scope comes with everything before)."""
    mine = [
        i
        for i in items
        if (i["type"] == "op" and i["op"]["record"] == record)
        or (i["type"] == "snapshot" and i["snapshot"]["record"] == record)
    ]
    return len(mine) > 1 or any(i["type"] == "snapshot" for i in mine)


def _scope_change(server: Callable[[], str], awa: str) -> str:
    """Two raw probe devices of moussa's, at the same cursor, pull every scope event one from each
    server, and must get identical items: the thies record moving into zone dakar (its history)
    and back out (an exit), each move pushed once through each server; then moussa's token
    gaining zone thies (scope delta: its history), losing it (an exit) and gaining it again.
    Returns the token with thies, for the fleet."""
    writer = LocalWriter(schema, "probe-awa2", _now)
    # The thies record must be visible to nobody but thies: no agent, zone thies.
    ops = [
        encode_op(writer.assign(THIES, "agent", None)),
        encode_op(writer.assign(THIES, "zone", "thies")),
    ]
    pushed = _push(server(), awa, "probe-awa2", ops)
    assert len(pushed["acked"]) == 2, pushed
    old = token_for(server(), "moussa", ["dakar"])
    new = token_for(server(), "moussa", ["dakar", "thies"])
    pair = ("probe-m-a", "probe-m-b")
    # Every request moves the cursor (it registers the device: a transaction), not the feed.
    cursor = max(_pull_all(s, old, d, 0) for s, d in zip(("ts", "py"), pair, strict=True))

    def both(token: str, order: tuple[str, str], what: str) -> list[Any]:
        nonlocal cursor
        got = [_pages(s, token, d, cursor) for s, d in zip(order, pair, strict=True)]
        a, b = (canonical_json(items) for items, _ in got)
        assert a == b, f"{what} answered differently:\n{order[0]}: {a}\n{order[1]}: {b}"
        cursor = max(c for _, c in got)
        return got[0][0]

    for via in ("ts", "py"):
        moved = _push(via, awa, "probe-awa2", [encode_op(writer.assign(THIES, "zone", "dakar"))])
        assert len(moved["acked"]) == 1, moved
        assert _has(both(old, ("ts", "py"), f"a move into dakar pushed via {via}"), THIES)
        moved = _push(via, awa, "probe-awa2", [encode_op(writer.assign(THIES, "zone", "thies"))])
        assert len(moved["acked"]) == 1, moved
        items = both(old, ("py", "ts"), f"a move out of dakar pushed via {via}")
        assert {"type": "exit", "record": THIES} in items, items

    for token, order, what in (
        (new, ("ts", "py"), "a token gaining thies"),
        (old, ("py", "ts"), "a token losing thies"),
        (new, ("py", "ts"), "a token gaining thies again"),
    ):
        items = both(token, order, what)
        if token == old:
            assert {"type": "exit", "record": THIES} in items, items
        else:
            assert _has(items, THIES), items
    return new


def test_lone_surrogate_in_an_op_value_is_answered_alike() -> None:
    """A lone surrogate (valid JSON text: "\\ud800") inside an op value, sent to each server.
    PostgreSQL's jsonb cannot store it; both servers must answer the same way and store
    nothing, and keep serving afterwards."""
    url = recreate_database()
    migrate_py(url)
    answers: dict[str, tuple[int, Any]] = {}
    with both_servers(url, "surrogate"):
        token = token_for("ts", "awa", ["dakar"])
        for name in sorted(SERVERS):
            device = f"surrogate-{name}"
            writer = LocalWriter(schema, device, _now)
            ops = [
                encode_op(writer.assign("dossier:s", "zone", "dakar")),
                encode_op(writer.assign("dossier:s", "client_name", "a\ud800b")),
            ]
            res = httpx.post(
                f"{SERVERS[name]}/v1/push",
                content=json.dumps({"ops": ops}),  # ensure_ascii: the surrogate is escaped
                headers={
                    "Authorization": f"Bearer {token}",
                    "Accord-Device": device,
                    "Content-Type": "application/json",
                },
                timeout=60,
            )
            answers[name] = (res.status_code, res.json())
            # Still serving: the same device's next push of a plain op works.
            ok = encode_op(writer.assign("dossier:s", "zone", "dakar"))
            assert _push(name, token, device, [ok])["acked"] == [ok["op_id"]]
        print(json.dumps({"lone_surrogate": answers}))
        stored = query(url, "select op_id from feed where op::text like '%%client_name%%'")
        assert stored == [], stored
    assert answers["ts"][0] == answers["py"][0], answers


@pytest.mark.parametrize("name", sorted(SERVERS))
@pytest.mark.xfail(strict=True, reason="known bug in both servers: a lost scope delta is lost")
def test_a_lost_scope_delta_is_sent_again(name: str) -> None:
    """A device whose read keys changed gets the history of the records entering its scope in its
    next pull (ADR-0011). If that answer is lost, the retry (same cursor) must carry it again;
    both servers record the new keys with the first answer and never send it again."""
    url = recreate_database()
    migrate_py(url)
    with both_servers(url, f"lost-delta-{name}"):
        awa = token_for("ts", "awa", ["thies"])
        writer = LocalWriter(schema, "awa-1", _now)
        ops = [
            encode_op(writer.assign(THIES, "zone", "thies")),
            encode_op(writer.inc(THIES, "visits", 2)),
        ]
        assert len(_push(name, awa, "awa-1", ops)["acked"]) == 2
        old = token_for(name, "moussa", ["dakar"])
        new = token_for(name, "moussa", ["dakar", "thies"])
        cursor = _pull_all(name, old, "moussa-1", 0)
        first, _ = _pages(name, new, "moussa-1", cursor)
        assert _has(first, THIES), first  # the delta... and its answer is lost
        again, _ = _pages(name, new, "moussa-1", cursor)
        assert _has(again, THIES), f"{name}: the retried pull has no history: {again}"
