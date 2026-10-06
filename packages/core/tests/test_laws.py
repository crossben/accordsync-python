"""Strategy laws, checked against reference models. Port of `laws.test.ts`, with a seeded
generator in place of fast-check.

Random devices write and partially sync. Every op ever made is then replayed into fresh replicas in
shuffled orders, with duplicates. The tests assert order independence and idempotency, convergence,
that each strategy matches its definition, and that compaction (snapshot, then the remaining ops)
reads the same as the full log.
"""

import functools
import itertools
import json
import os
import random
from collections.abc import Iterable
from dataclasses import dataclass

from accordsync_core import (
    AddOp,
    AssignOp,
    FieldRef,
    IncOp,
    LocalWriter,
    Op,
    RecordSnapshot,
    RemoveOp,
    Replica,
    compare_hlc,
    conflict,
    counter,
    define_schema,
    lww,
    set_,
)
from accordsync_core._text import utf16_key

SCHEMA = define_schema(
    {"dossier": {"name": lww(), "docs": set_(), "visits": counter(), "status": conflict()}}
)
RUNS = int(os.environ.get("ACCORD_PROPERTY_RUNS", "300"))


@dataclass
class Scenario:
    devs: list[LocalWriter]
    all: list[Op]


def scenario(rnd: random.Random) -> Scenario:
    tick = [1700000000000]

    def make(i: int) -> LocalWriter:
        # Phone clocks are wrong: each device runs up to an hour fast or slow.
        skew = rnd.randint(-3600000, 3600000)

        def now() -> int:
            tick[0] += 7
            return tick[0] + skew

        # Mixed case device ids: op ids must sort by code unit, never case-folded.
        return LocalWriter(SCHEMA, ["d", "D", "_", "e"][i] + str(i), now)

    devs = [make(i) for i in range(rnd.randint(2, 4))]
    ops: list[Op] = []
    for _ in range(rnd.randint(0, 60)):
        if rnd.random() < 0.5:
            w = rnd.choice(devs)
            rec = f"dossier:{rnd.randrange(2)}"
            v = rnd.randint(-3, 3)
            match rnd.randrange(4):
                case 0:
                    ops.append(w.assign(rec, "name", f"n{v}"))
                case 1:
                    ops.append(w.assign(rec, "status", f"s{v}"))
                case 2:
                    ops.append(w.inc(rec, "visits", v))
                case _:
                    el = ["doc0", "Doc1", "😀", ""][abs(v) % 4]
                    write = w.remove if rnd.random() < 0.5 else w.add
                    ops.append(write(rec, "docs", el))
        else:
            # Deliver an arbitrary subset, in log order: partial syncs and lost messages.
            src, dst = rnd.choice(devs), rnd.choice(devs)
            for op in src.replica.ops():
                if rnd.random() < 0.5:
                    dst.receive(op)
    return Scenario(devs, ops)


def replay(ops: Iterable[Op]) -> Replica:
    r = Replica(SCHEMA)
    for op in ops:
        r.apply(op)
    return r


def model(ops: list[Op], record: str) -> dict[str, object]:
    """Reference models, computed straight from the definitions over the full op set."""
    mine = [o for o in ops if o.record == record]
    names = [o for o in mine if o.field == "name" and isinstance(o, AssignOp)]
    winner = None
    for o in names:
        if winner is None or compare_hlc(o.hlc, winner.hlc) > 0:
            winner = o
    visits = sum(o.by for o in mine if isinstance(o, IncOp))
    docs_ops = [o for o in mine if isinstance(o, AddOp | RemoveOp)]
    removed = {d for o in docs_ops for d in o.deps}
    docs = {o.element for o in docs_ops if isinstance(o, AddOp) and o.op_id not in removed}
    statuses = [o for o in mine if o.field == "status" and isinstance(o, AssignOp)]
    superseded = {d for o in statuses for d in o.deps}
    live = sorted(
        (o for o in statuses if o.op_id not in superseded), key=lambda o: utf16_key(o.op_id)
    )
    out: dict[str, object] = {
        "docs": sorted((str(d) for d in docs), key=utf16_key),
        "visits": visits,
    }
    if winner is not None:
        out["name"] = winner.value
    if len(live) == 1:
        out["status"] = {"value": live[0].value}
    elif live:
        out["status"] = {"conflicted": [{"value": o.value, "opId": o.op_id} for o in live]}
    return out


def test_any_delivery_order_with_duplicates_reads_the_same_state() -> None:
    for seed in range(RUNS):
        rnd = random.Random(seed)
        ops = scenario(rnd).all
        reference = replay(ops).snapshot()
        for _ in range(3):
            order = ops + ops[: rnd.randrange(5)]
            rnd.shuffle(order)
            assert replay(order).snapshot() == reference, f"seed {seed}"


def test_devices_converge_once_every_op_is_delivered() -> None:
    for seed in range(RUNS):
        rnd = random.Random(seed)
        s = scenario(rnd)
        for d in s.devs:
            order = list(s.all)
            rnd.shuffle(order)
            for op in order:
                d.receive(op)
        reference = replay(s.all).snapshot()
        for d in s.devs:
            assert d.replica.snapshot() == reference, f"seed {seed}"


def test_each_strategy_matches_its_definition() -> None:
    for seed in range(RUNS):
        rnd = random.Random(seed)
        ops = scenario(rnd).all
        order = list(ops)
        rnd.shuffle(order)
        r = replay(order)
        for record in r.records():
            assert r.read(record) == model(ops, record), f"seed {seed}, {record}"


def test_compaction_a_snapshot_through_json_plus_later_ops_reads_like_the_whole_log() -> None:
    for seed in range(RUNS):
        rnd = random.Random(seed)
        ops = scenario(rnd).all
        # Ops in creation order are causally ordered, so every prefix is causally closed.
        cut = rnd.randint(0, len(ops))
        before = replay(ops[:cut])
        compacted = Replica(SCHEMA)
        for record in before.records():
            # Through JSON, as a snapshot travels from the server.
            text = json.dumps(before.snapshot_record(record).to_json())
            compacted.load_snapshot(RecordSnapshot.from_json(json.loads(text)))
        for op in ops[cut:]:
            compacted.apply(op)
        assert compacted.snapshot() == replay(ops).snapshot(), f"seed {seed}, cut {cut}"


def test_a_conflict_field_written_concurrently_is_never_auto_resolved() -> None:
    rnd = random.Random(7)
    for _ in range(RUNS):
        x, y = f"x{rnd.randrange(1000)}", f"y{rnd.randrange(1000)}"
        now = functools.partial(next, itertools.count())
        a, b = LocalWriter(SCHEMA, "a", now), LocalWriter(SCHEMA, "b", now)
        ops = [a.assign("dossier:1", "status", x), b.assign("dossier:1", "status", y)]
        a.receive(ops[1])
        b.receive(ops[0])
        for w in (a, b):
            assert w.replica.conflicts() == [FieldRef("dossier:1", "status")]


def test_load_snapshot_keeps_unsynced_local_ops_and_without_keeps_bases() -> None:
    a = LocalWriter(SCHEMA, "a", lambda: 1)
    a.inc("dossier:1", "visits", 1)
    server = replay(a.replica.ops())
    local = a.inc("dossier:1", "visits", 10)
    a.replica.load_snapshot(server.snapshot_record("dossier:1"), keep={local.op_id})
    assert a.replica.size == 1
    assert a.replica.bases() == [server.snapshot_record("dossier:1")]
    read = a.replica.read("dossier:1")
    assert read is not None
    assert read["visits"] == 11
    rolled = a.replica.without({local.op_id})
    assert rolled.read("dossier:1") == {"docs": [], "visits": 1}
