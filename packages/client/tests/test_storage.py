"""The storage adapter contract (port of storage.test.ts / the Dart storage_contract.dart), run on
MemoryStorage and SqliteStorage."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from accordsync import (
    MemoryStorage,
    RecordSnapshot,
    SqliteStorage,
    StorageAdapter,
    StorageTx,
    StoredMeta,
)


def op(n: int) -> dict[str, Any]:
    return {
        "op_id": f"d:{n}",
        "record": "dossier:1",
        "field": "visits",
        "kind": "inc",
        "by": n,
        "hlc": f"{1000 + n}:00000:d",
    }


def meta(cursor: int) -> StoredMeta:
    return StoredMeta(device_id="d", cursor=cursor, hlc="1000:00000:d", seq=3)


def snap(n: int) -> RecordSnapshot:
    return RecordSnapshot("dossier:1", {"visits": {"strategy": "counter", "total": n}})


@dataclass
class Made:
    store: StorageAdapter
    reopen: Callable[[], StorageAdapter] | None


@pytest.fixture(params=["memory", "sqlite-file", "sqlite-connection"])
def make(request: pytest.FixtureRequest, tmp_path: Path) -> Callable[[], Made]:
    count = 0

    def factory() -> Made:
        nonlocal count
        count += 1
        if request.param == "memory":
            return Made(MemoryStorage(), None)
        path = tmp_path / f"accord-{count}.db"
        if request.param == "sqlite-connection":
            conn = sqlite3.connect(path, check_same_thread=False)
            return Made(SqliteStorage(conn), lambda: SqliteStorage(path))
        return Made(SqliteStorage(path), lambda: SqliteStorage(path))

    return factory


def test_starts_empty(make: Callable[[], Made]) -> None:
    s = make().store.load()
    assert s.meta is None
    assert list(s.snapshots) == []
    assert list(s.ops) == []
    assert list(s.outbox) == []


def test_commits_and_applies_deletes_clears_and_outbox_removals(make: Callable[[], Made]) -> None:
    store = make().store
    store.commit(StorageTx(put_ops=[op(1), op(2), op(3)], outbox_add=["d:1", "d:2"], meta=meta(5)))
    store.commit(StorageTx(delete_ops=["d:3"], outbox_delete=["d:1"], meta=meta(7)))
    s = store.load()
    assert sorted(o["op_id"] for o in s.ops) == ["d:1", "d:2"]
    assert list(s.outbox) == ["d:2"]
    assert s.meta == meta(7)

    store.commit(StorageTx(clear_ops=True, put_ops=[op(9)]))
    assert list(store.load().ops) == [op(9)]
    assert store.load().meta == meta(7)  # a commit without meta keeps it


def test_stores_snapshots_replaced_by_record_cleared_with_ops(make: Callable[[], Made]) -> None:
    store = make().store
    store.commit(StorageTx(put_snapshots=[snap(1)]))
    store.commit(StorageTx(put_snapshots=[snap(2)]))
    assert [s.to_json() for s in store.load().snapshots] == [snap(2).to_json()]
    store.commit(StorageTx(delete_snapshots=["dossier:1"]))
    assert list(store.load().snapshots) == []
    store.commit(StorageTx(put_snapshots=[snap(3)], put_ops=[op(1)]))
    store.commit(StorageTx(clear_ops=True))
    s = store.load()
    assert list(s.snapshots) == []
    assert list(s.ops) == []


def test_returns_copies(make: Callable[[], Made]) -> None:
    store = make().store
    o = op(1)
    s = snap(1)
    store.commit(StorageTx(put_ops=[o], put_snapshots=[s]))
    o["by"] = 99
    s.fields["visits"]["total"] = 99
    loaded = store.load()
    assert list(loaded.ops) == [op(1)]
    assert loaded.snapshots[0].to_json() == snap(1).to_json()
    loaded.ops[0]["by"] = 98
    loaded.snapshots[0].fields["visits"]["total"] = 98
    again = store.load()
    assert list(again.ops) == [op(1)]
    assert again.snapshots[0].to_json() == snap(1).to_json()


def test_survives_a_restart(make: Callable[[], Made]) -> None:
    made = make()
    if made.reopen is None:
        pytest.skip("in-memory storage does not survive a restart")
    made.store.commit(
        StorageTx(put_ops=[op(1)], outbox_add=["d:1"], put_snapshots=[snap(4)], meta=meta(2))
    )
    made.store.close()
    again = made.reopen().load()
    assert again.meta == meta(2)
    assert list(again.ops) == [op(1)]
    assert list(again.outbox) == ["d:1"]
    assert [s.to_json() for s in again.snapshots] == [snap(4).to_json()]


def test_sqlite_commit_is_atomic(tmp_path: Path) -> None:
    path = tmp_path / "accord.db"
    store = SqliteStorage(path)
    store.commit(StorageTx(put_ops=[op(1)], meta=meta(1)))
    # A trigger makes the outbox insert fail, after the op insert succeeded.
    raw = sqlite3.connect(path)
    raw.execute(
        "create trigger boom before insert on accord_outbox "
        "begin select raise(abort, 'disk full'); end"
    )
    raw.commit()
    raw.close()
    with pytest.raises(sqlite3.IntegrityError, match="disk full"):
        store.commit(
            StorageTx(put_ops=[op(2)], delete_ops=["d:1"], outbox_add=["d:2"], meta=meta(9))
        )
    s = store.load()
    assert list(s.ops) == [op(1)]
    assert list(s.outbox) == []
    assert s.meta == meta(1)
    # And the connection is usable again.
    store.commit(StorageTx(put_ops=[op(3)]))
    assert sorted(o["op_id"] for o in store.load().ops) == ["d:1", "d:3"]


def test_sqlite_uses_the_typescript_tables_and_json(tmp_path: Path) -> None:
    path = tmp_path / "accord.db"
    store = SqliteStorage(path)
    store.commit(
        StorageTx(put_ops=[op(1)], outbox_add=["d:1"], put_snapshots=[snap(2)], meta=meta(4))
    )
    store.close()
    raw = sqlite3.connect(path)
    tables = {r[0] for r in raw.execute("select name from sqlite_master where type = 'table'")}
    assert tables == {"accord_ops", "accord_outbox", "accord_snapshots", "accord_meta"}
    (v,) = raw.execute("select v from accord_meta where k = 'meta'").fetchone()
    assert json.loads(v) == {"deviceId": "d", "cursor": 4, "hlc": "1000:00000:d", "seq": 3}
    (body,) = raw.execute("select body from accord_ops where op_id = 'd:1'").fetchone()
    assert json.loads(body) == op(1)
    assert " " not in body  # compact, like JSON.stringify
    (sbody,) = raw.execute("select body from accord_snapshots").fetchone()
    assert json.loads(sbody) == snap(2).to_json()
    raw.close()


def test_sqlite_stores_lone_surrogates(tmp_path: Path) -> None:
    store = SqliteStorage(tmp_path / "accord.db")
    o = {**op(1), "kind": "assign", "value": "\ud800x", "deps": []}
    del o["by"]
    store.commit(StorageTx(put_ops=[o]))
    assert list(store.load().ops) == [o]


def test_sqlite_storage_is_thread_safe(tmp_path: Path) -> None:
    import threading

    store = SqliteStorage(tmp_path / "accord.db")

    def work(base: int) -> None:
        for i in range(25):
            store.commit(StorageTx(put_ops=[op(base + i)], outbox_add=[f"d:{base + i}"]))

    threads = [threading.Thread(target=work, args=(k * 100 + 1,)) for k in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    s = store.load()
    assert len(s.ops) == 100
    assert len(s.outbox) == 100
