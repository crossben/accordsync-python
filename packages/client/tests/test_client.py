"""Port of the Dart client_test.dart (itself a port of the client's e2e.test.ts), against an
in-memory server (FakeServer)."""

from __future__ import annotations

import json
import random
import threading
import time
from collections.abc import Callable, Sequence
from typing import Any

import pytest
from accordsync import (
    PROTOCOL_VERSION,
    AccordClient,
    ConflictInfo,
    ConflictValue,
    HttpError,
    MemoryStorage,
    PullResult,
    PushResult,
    Refusal,
    StorageAdapter,
    Transport,
    WireOp,
    conflict,
    counter,
    define_schema,
    lww,
    set_,
)

from .fake_server import Access, FakeServer

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

Open = Callable[..., AccordClient]


class World:
    def __init__(self) -> None:
        self.zones: dict[str, list[str]] = {
            "awa": ["dakar"],
            "moussa": ["dakar"],
            "fatou": ["thies"],
        }

        def scopes(_: str, f: dict[str, object]) -> list[str]:
            out = []
            if isinstance(f.get("agent"), str):
                out.append(f"agent:{f['agent']}")
            if isinstance(f.get("zone"), str):
                out.append(f"zone:{f['zone']}")
            return out

        def access(user: str) -> Access:
            keys = [f"agent:{user}", *(f"zone:{z}" for z in self.zones[user])]
            return Access(read=keys, write=keys)

        self.server = FakeServer(schema=schema, scopes=scopes, access=access)

    def open(
        self,
        user: str,
        device_id: str,
        *,
        storage: StorageAdapter | None = None,
        transport: Transport | None = None,
        pull_limit: int = 500,
        push_batch: int = 200,
    ) -> AccordClient:
        return AccordClient.open(
            schema=schema,
            storage=storage if storage is not None else MemoryStorage(),
            transport=transport if transport is not None else self.server.transport_for(user),
            device_id=device_id,
            pull_limit=pull_limit,
            push_batch=push_batch,
        )


@pytest.fixture
def world() -> World:
    return World()


def sync_all(cs: Sequence[AccordClient]) -> None:
    for _ in range(2):
        for c in cs:
            c.sync()


class Spy:
    def __init__(self, inner: Transport) -> None:
        self.inner = inner
        self.pushed: list[str] = []
        self.pulls: list[int] = []
        self.during_pull: Callable[[], object] | None = None
        """Runs while a pull is in flight (after the round's push): a write made in that window."""

    def push(self, device_id: str, ops: Sequence[WireOp]) -> PushResult:
        self.pushed.extend(str(o["op_id"]) for o in ops)
        return self.inner.push(device_id, ops)

    def pull(self, device_id: str, cursor: int, limit: int) -> PullResult:
        self.pulls.append(cursor)
        result = self.inner.pull(device_id, cursor, limit)
        hook, self.during_pull = self.during_pull, None
        if hook is not None:
            hook()
        return result


def test_speaks_protocol_version_1() -> None:
    assert PROTOCOL_VERSION == 1


def test_two_agents_edit_offline_then_converge(world: World) -> None:
    awa = world.open("awa", "awa-phone")
    moussa = world.open("moussa", "moussa-phone")
    awa.assign("dossier:1", "zone", "dakar")
    awa.assign("dossier:1", "agent", "awa")
    awa.sync()
    moussa.sync()

    awa.inc("dossier:1", "visits", 2)
    awa.add("dossier:1", "docs", "cni.pdf")
    moussa.inc("dossier:1", "visits", 3)
    moussa.assign("dossier:1", "client_name", "Aminata Fall")

    sync_all([awa, moussa])
    for c in (awa, moussa):
        assert c.read("dossier:1") == {
            "agent": "awa",
            "zone": "dakar",
            "client_name": "Aminata Fall",
            "visits": 5,
            "docs": ["cni.pdf"],
        }
        assert c.status().pending == 0


def test_conflict_surfaces_on_both_devices_and_a_resolution_clears_it(world: World) -> None:
    awa = world.open("awa", "awa-phone")
    moussa = world.open("moussa", "moussa-phone")
    awa.assign("dossier:1", "zone", "dakar")
    sync_all([awa, moussa])

    awa.assign("dossier:1", "status", "approved")
    moussa.assign("dossier:1", "status", "rejected")
    sync_all([awa, moussa])
    for c in (awa, moussa):
        assert c.conflicts() == [
            ConflictInfo(
                "dossier:1",
                "status",
                (
                    ConflictValue("approved", "awa-phone:2"),
                    ConflictValue("rejected", "moussa-phone:1"),
                ),
            )
        ]

    moussa.resolve("dossier:1", "status", "approved")
    sync_all([awa, moussa])
    for c in (awa, moussa):
        assert c.conflicts() == []
        assert (c.read("dossier:1") or {})["status"] == {"value": "approved"}


def test_refused_write_is_rolled_back_and_reported(world: World) -> None:
    fatou = world.open("fatou", "fatou-phone")
    awa = world.open("awa", "awa-phone")
    fatou.assign("dossier:7", "zone", "thies")
    fatou.sync()

    refusals: list[Refusal] = []
    changes: list[list[str]] = []
    awa.on("refused", refusals.append)
    awa.assign("dossier:7", "client_name", "not mine")
    assert (awa.read("dossier:7") or {})["client_name"] == "not mine"  # local-first
    awa.on("change", changes.append)
    awa.sync()
    assert refusals == [
        Refusal(
            "awa-phone:1", "dossier:7", "client_name", "out of scope: you may not write dossier:7"
        )
    ]
    assert ["dossier:7"] in changes
    assert awa.read("dossier:7") is None
    assert awa.status().pending == 0


def test_refusal_rollback_is_persisted(world: World) -> None:
    storage = MemoryStorage()
    fatou = world.open("fatou", "fatou-phone")
    fatou.assign("dossier:7", "zone", "thies")
    fatou.sync()
    awa = world.open("awa", "awa-phone", storage=storage)
    awa.assign("dossier:7", "client_name", "not mine")
    awa.sync()
    assert list(storage.load().ops) == []
    again = world.open("awa", "x", storage=storage)
    assert again.read("dossier:7") is None


def test_record_reassigned_away_is_removed(world: World) -> None:
    awa = world.open("awa", "awa-phone")
    fatou = world.open("fatou", "fatou-phone")
    awa.assign("dossier:3", "agent", "awa")
    awa.inc("dossier:3", "visits", 4)
    awa.sync()

    awa.assign("dossier:3", "agent", "fatou")
    changed: list[list[str]] = []
    awa.on("change", changed.append)
    awa.sync()
    assert awa.read("dossier:3") is None
    assert ["dossier:3"] in changed

    fatou.sync()
    got = fatou.read("dossier:3") or {}
    assert got["agent"] == "fatou"
    assert got["visits"] == 4


def test_exit_is_persisted(world: World) -> None:
    storage = MemoryStorage()
    awa = world.open("awa", "awa-phone", storage=storage)
    awa.assign("dossier:3", "agent", "awa")
    awa.sync()
    awa.assign("dossier:3", "agent", "fatou")
    awa.sync()
    assert list(storage.load().ops) == []
    assert world.open("awa", "x", storage=storage).read("dossier:3") is None


def test_follows_scope_change_without_resync_keeping_unpushed_edits(world: World) -> None:
    fatou = world.open("fatou", "fatou-phone")
    fatou.assign("dossier:8", "zone", "thies")
    fatou.sync()
    awa = world.open("awa", "awa-phone")
    awa.assign("dossier:1", "zone", "dakar")
    awa.sync()
    assert awa.records() == ["dossier:1"]

    world.zones["awa"] = ["dakar", "thies"]
    awa.inc("dossier:8", "visits", 1)
    resyncs: list[None] = []
    awa.on("resync", resyncs.append)
    awa.sync()
    assert resyncs == []
    assert awa.records() == ["dossier:1", "dossier:8"]
    assert awa.records("dossier") == ["dossier:1", "dossier:8"]
    assert awa.records("other") == []
    assert awa.read("dossier:8") == {"zone": "thies", "visits": 1, "docs": []}
    fatou.sync()
    assert (fatou.read("dossier:8") or {})["visits"] == 1


def test_resync_required_pushes_then_reloads_from_zero(world: World) -> None:
    storage = MemoryStorage()
    awa = world.open("awa", "awa-phone", storage=storage)
    awa.assign("dossier:1", "zone", "dakar")
    awa.sync()
    awa.inc("dossier:1", "visits", 2)
    world.server.require_resync("awa-phone")
    resyncs: list[None] = []
    awa.on("resync", resyncs.append)
    awa.sync()
    assert resyncs == [None]
    assert awa.status().pending == 0
    assert awa.read("dossier:1") == {"zone": "dakar", "visits": 2, "docs": []}
    again = world.open("awa", "x", storage=storage)
    assert again.read("dossier:1") == {"zone": "dakar", "visits": 2, "docs": []}
    assert again.status().cursor == awa.status().cursor


def test_survives_restart(world: World) -> None:
    storage = MemoryStorage()
    first = world.open("awa", "awa-tablet", storage=storage)
    first.assign("dossier:1", "zone", "dakar")
    first.sync()
    first.inc("dossier:1", "visits", 1)  # offline, then the app is killed
    cursor_before = first.status().cursor
    assert cursor_before > 0
    first.close()

    again = world.open("awa", "other-id", storage=storage)
    assert again.device_id == "awa-tablet"
    assert again.status().pending == 1
    assert again.status().cursor == cursor_before
    assert (again.read("dossier:1") or {})["visits"] == 1
    assert again.inc("dossier:1", "visits", 1).op_id == "awa-tablet:3"
    again.sync()
    assert again.status().pending == 0


def test_writes_during_a_sync_round_are_not_lost(world: World) -> None:
    spy = Spy(world.server.transport_for("awa"))
    awa = world.open("awa", "awa-phone", transport=spy)
    awa.assign("dossier:1", "zone", "dakar")
    spy.during_pull = lambda: awa.inc("dossier:1", "visits", 1)
    awa.sync()
    awa.sync()
    other = world.open("awa", "awa-laptop")
    other.sync()
    assert (other.read("dossier:1") or {})["visits"] == 1


def test_writes_from_another_thread_during_a_round_are_not_lost(world: World) -> None:
    gate = threading.Event()
    release = threading.Event()

    class Slow(Spy):
        def push(self, device_id: str, ops: Sequence[WireOp]) -> PushResult:
            gate.set()
            release.wait(5)
            return super().push(device_id, ops)

    awa = world.open("awa", "awa-phone", transport=Slow(world.server.transport_for("awa")))
    awa.assign("dossier:1", "zone", "dakar")
    errors: list[BaseException] = []

    def run() -> None:
        try:
            awa.sync()
        except BaseException as e:  # pragma: no cover
            errors.append(e)

    t = threading.Thread(target=run)
    t.start()
    assert gate.wait(5)
    awa.inc("dossier:1", "visits", 1)  # the lock is not held during network I/O
    release.set()
    t.join(5)
    assert errors == []
    awa.sync()
    other = world.open("awa", "awa-laptop")
    other.sync()
    assert (other.read("dossier:1") or {})["visits"] == 1


def test_concurrent_sync_calls_share_one_round(world: World) -> None:
    gate = threading.Event()
    release = threading.Event()

    class Slow(Spy):
        def pull(self, device_id: str, cursor: int, limit: int) -> PullResult:
            gate.set()
            release.wait(5)
            return super().pull(device_id, cursor, limit)

    spy = Slow(world.server.transport_for("awa"))
    awa = world.open("awa", "awa-phone", transport=spy)
    synced: list[int] = []
    awa.on("synced", synced.append)
    threads = [threading.Thread(target=awa.sync) for _ in range(3)]
    threads[0].start()
    assert gate.wait(5)
    for t in threads[1:]:
        t.start()
    release.set()
    for t in threads:
        t.join(5)
    assert len(spy.pulls) == 1
    assert len(synced) == 1


def test_pushes_in_write_order_in_batches_and_pulls_every_page(world: World) -> None:
    spy = Spy(world.server.transport_for("awa"))
    batches: list[int] = []
    inner_push = spy.push

    def push(device_id: str, ops: Sequence[WireOp]) -> PushResult:
        batches.append(len(ops))
        return inner_push(device_id, ops)

    spy.push = push  # type: ignore[method-assign]
    awa = world.open("awa", "awa-phone", transport=spy, push_batch=3)
    awa.assign("dossier:1", "zone", "dakar")
    for _ in range(9):
        awa.inc("dossier:1", "visits", 1)
    awa.sync()
    assert spy.pushed == [f"awa-phone:{i}" for i in range(1, 11)]
    assert batches == [3, 3, 3, 1]

    reader_spy = Spy(world.server.transport_for("moussa"))
    reader = world.open("moussa", "moussa-phone", transport=reader_spy, pull_limit=4)
    reader.sync()
    assert (reader.read("dossier:1") or {})["visits"] == 9
    assert reader.status().cursor == 10
    assert reader_spy.pulls == [0, 4, 8]


def test_outbox_reloaded_in_write_order(world: World) -> None:
    storage = MemoryStorage()
    awa = world.open("awa", "awa-phone", storage=storage)
    awa.assign("dossier:1", "zone", "dakar")
    for _ in range(11):
        awa.inc("dossier:1", "visits", 1)
    awa.close()
    spy = Spy(world.server.transport_for("awa"))
    again = world.open("awa", "awa-phone", storage=storage, transport=spy)
    again.sync()
    assert spy.pushed == [f"awa-phone:{i}" for i in range(1, 13)]


@pytest.mark.parametrize("seed", [1, 2, 3, 4, 5])
def test_random_work_by_three_devices_converges(world: World, seed: int) -> None:
    rng = random.Random(seed)  # noqa: S311 (seeded, reproducible)
    devices = [
        world.open("awa", "awa-1"),
        world.open("awa", "awa-2"),
        world.open("moussa", "moussa-1", pull_limit=7),
    ]
    devices[0].assign("dossier:1", "zone", "dakar")
    devices[0].assign("dossier:2", "zone", "dakar")
    sync_all(devices)
    for _ in range(80):
        d = rng.choice(devices)
        record = f"dossier:{1 + rng.randrange(2)}"
        match rng.randrange(6):
            case 0:
                d.inc(record, "visits", rng.randrange(8) - 2)
            case 1:
                d.add(record, "docs", f"doc-{rng.randrange(4)}")
            case 2:
                d.remove(record, "docs", f"doc-{rng.randrange(4)}")
            case 3:
                d.assign(record, "status", f"s{rng.randrange(3)}")
            case 4:
                d.assign(record, "client_name", f"n{rng.randrange(10)}")
            case _:
                d.sync()
    sync_all(devices)
    states = {json.dumps([d.read(r) for r in ("dossier:1", "dossier:2")]) for d in devices}
    assert len(states) == 1


def test_compaction_snapshot_restart_and_merge_on_top(world: World) -> None:
    awa = world.open("awa", "awa-phone")
    awa.assign("dossier:1", "zone", "dakar")
    for _ in range(4):
        awa.inc("dossier:1", "visits", 1)
    awa.add("dossier:1", "docs", "a.pdf")
    awa.remove("dossier:1", "docs", "a.pdf")
    awa.assign("dossier:1", "status", "submitted")
    sync_all([awa])
    world.server.compact("dossier:1")

    storage = MemoryStorage()
    tablet = world.open("awa", "awa-tablet", storage=storage)
    tablet.inc("dossier:1", "visits", 10)  # a blind offline write to an unseen record
    tablet.sync()
    assert tablet.read("dossier:1") == {**(awa.read("dossier:1") or {}), "visits": 14}
    snaps = storage.load().snapshots
    assert [s.record for s in snaps] == ["dossier:1"]
    tablet.close()

    again = world.open("awa", "x", storage=storage)
    assert again.read("dossier:1") == {
        "zone": "dakar",
        "visits": 14,
        "docs": [],
        "status": {"value": "submitted"},
    }
    awa.sync()
    assert (awa.read("dossier:1") or {})["visits"] == 14


def test_lost_storage_keeps_old_ops_and_never_reuses_an_op_id(world: World) -> None:
    before = world.open("awa", "awa-tablet")
    before.assign("dossier:1", "zone", "dakar")
    before.inc("dossier:1", "visits", 2)
    before.sync()
    after = world.open("awa", "awa-tablet")
    after.sync()
    assert after.inc("dossier:1", "visits", 3).op_id == "awa-tablet:3"
    after.sync()
    assert after.status().pending == 0
    assert (after.read("dossier:1") or {})["visits"] == 5


def test_device_seq_after_compaction_never_reuses_an_op_id(world: World) -> None:
    before = world.open("awa", "awa-tablet")
    before.assign("dossier:1", "zone", "dakar")
    before.inc("dossier:1", "visits", 2)
    before.sync()
    world.server.compact("dossier:1")  # its own ops now arrive folded in a snapshot
    after = world.open("awa", "awa-tablet")
    after.sync()
    assert after.inc("dossier:1", "visits", 3).op_id == "awa-tablet:3"
    after.sync()
    assert after.status().pending == 0
    assert (after.read("dossier:1") or {})["visits"] == 5


def test_snapshot_arriving_with_an_unpushed_edit_keeps_it_on_top(world: World) -> None:
    awa = world.open("awa", "awa-phone")
    awa.assign("dossier:1", "zone", "dakar")
    awa.inc("dossier:1", "visits", 4)
    awa.sync()
    world.server.compact("dossier:1")

    spy = Spy(world.server.transport_for("awa"))
    storage = MemoryStorage()
    tablet = world.open("awa", "awa-tablet", transport=spy, storage=storage)
    tablet.sync()
    world.server.compact("dossier:1")
    spy.during_pull = lambda: tablet.inc("dossier:1", "visits", 1)
    tablet.sync()  # the snapshot lands while the inc is in the outbox
    assert tablet.status().pending == 1
    assert (tablet.read("dossier:1") or {})["visits"] == 5
    assert (world.open("awa", "y", storage=storage).read("dossier:1") or {})["visits"] == 5
    tablet.sync()
    awa.sync()
    assert (awa.read("dossier:1") or {})["visits"] == 5


def test_reinstalled_device_writing_before_first_sync_gets_a_refusal(world: World) -> None:
    before = world.open("awa", "awa-old")
    before.assign("dossier:1", "zone", "dakar")
    before.sync()
    after = world.open("awa", "awa-old")
    refusals: list[Refusal] = []
    after.on("refused", refusals.append)
    after.inc("dossier:1", "visits", 1)  # offline write, numbered 1 again
    after.sync()
    assert len(refusals) == 1
    assert "op id already used" in refusals[0].reason


def test_background_sync_with_backoff(world: World) -> None:
    world.server.fail_next = 2
    awa = AccordClient.open(
        schema=schema,
        storage=MemoryStorage(),
        transport=world.server.transport_for("awa"),
        device_id="awa-bg",
        min_backoff=0.01,
        max_backoff=0.04,
        sync_interval=0.02,
    )
    errors: list[BaseException] = []
    synced = threading.Event()
    awa.on("error", errors.append)
    awa.on("synced", lambda _: synced.set())
    awa.assign("dossier:1", "zone", "dakar")
    awa.start()
    assert synced.wait(5)
    awa.stop()
    assert len(errors) >= 1
    assert isinstance(errors[0], HttpError)
    assert awa.status().pending == 0
    assert awa.status().last_error is None
    assert awa.status().last_sync_at is not None
    awa.close()


def test_background_sync_picks_up_writes_soon(world: World) -> None:
    awa = AccordClient.open(
        schema=schema,
        storage=MemoryStorage(),
        transport=world.server.transport_for("awa"),
        device_id="awa-bg",
        sync_interval=60,
    )
    awa.start()
    first = threading.Event()
    awa.on("synced", lambda _: first.set())
    assert first.wait(5)
    second = threading.Event()
    awa.on("synced", lambda _: second.set())
    awa.assign("dossier:1", "zone", "dakar")
    assert second.wait(5)  # well before sync_interval
    assert awa.status().pending == 0
    awa.close()


def test_a_write_during_a_round_is_synced_soon_after_it(world: World) -> None:
    awa = AccordClient.open(
        schema=schema,
        storage=MemoryStorage(),
        transport=world.server.transport_for("awa"),
        device_id="awa-again",
        sync_interval=60,
    )
    wrote = threading.Event()

    def write_once(_: Any) -> None:
        if not wrote.is_set():
            wrote.set()
            awa.assign("dossier:1", "zone", "dakar")  # inside the round, after its push

    awa.on("synced", write_once)
    awa.start()
    assert wrote.wait(5)
    deadline = time.monotonic() + 3
    while awa.status().pending and time.monotonic() < deadline:
        time.sleep(0.02)
    assert awa.status().pending == 0  # well before sync_interval
    awa.close()


def test_a_write_during_a_manual_sync_is_synced_soon_after_it(world: World) -> None:
    awa = AccordClient.open(
        schema=schema,
        storage=MemoryStorage(),
        transport=world.server.transport_for("awa"),
        device_id="awa-again-manual",
        sync_interval=60,
    )
    first = threading.Event()
    awa.on("synced", lambda _: first.set())
    awa.start()
    assert first.wait(5)
    awa.sync()  # joins the first round if it is still ending, so the next one is ours
    wrote = threading.Event()

    def write_once(_: Any) -> None:
        if not wrote.is_set():
            wrote.set()
            awa.assign("dossier:1", "zone", "dakar")
            time.sleep(0.2)  # the loop wakes mid-round and joins it

    awa.on("synced", write_once)
    awa.sync()
    assert wrote.is_set()
    deadline = time.monotonic() + 3
    while awa.status().pending and time.monotonic() < deadline:
        time.sleep(0.02)
    assert awa.status().pending == 0
    awa.close()


def test_failed_round_keeps_the_outbox(world: World) -> None:
    awa = world.open("awa", "awa-phone")
    awa.assign("dossier:1", "zone", "dakar")
    world.server.fail_next = 1
    with pytest.raises(HttpError):
        awa.sync()
    assert awa.status().pending == 1
    awa.sync()
    assert awa.status().pending == 0


def test_listener_exceptions_are_logged_and_ignored(
    world: World, caplog: pytest.LogCaptureFixture
) -> None:
    awa = world.open("awa", "awa-phone")
    seen: list[Any] = []

    def boom(_: object) -> None:
        raise RuntimeError("listener bug")

    awa.on("change", boom)
    off = awa.on("change", seen.append)
    awa.assign("dossier:1", "zone", "dakar")
    assert seen == [["dossier:1"]]
    assert "listener raised" in caplog.text
    off()
    awa.assign("dossier:1", "zone", "thies")
    assert seen == [["dossier:1"]]
    with pytest.raises(ValueError, match="unknown event"):
        awa.on("nope", seen.append)  # type: ignore[arg-type]


def test_survives_a_restart_on_sqlite(world: World, tmp_path: Any) -> None:
    from accordsync import SqliteStorage

    path = tmp_path / "accord.db"
    first = world.open("awa", "awa-tablet", storage=SqliteStorage(path))
    first.assign("dossier:1", "zone", "dakar")
    first.add("dossier:1", "docs", "a.pdf")
    first.sync()
    world.server.compact("dossier:1")
    first.inc("dossier:1", "visits", 2)
    first.sync()
    first.inc("dossier:1", "visits", 1)
    first.close()
    again = world.open("awa", "x", storage=SqliteStorage(path))
    assert again.device_id == "awa-tablet"
    assert again.status().pending == 1
    assert again.read("dossier:1") == {"zone": "dakar", "visits": 3, "docs": ["a.pdf"]}
    assert again.inc("dossier:1", "visits", 1).op_id == "awa-tablet:5"
    again.sync()
    assert again.status().pending == 0
