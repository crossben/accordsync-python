"""In-memory storage: for tests, and for apps that accept losing unsynced writes on restart."""

from __future__ import annotations

import json
import threading
from typing import Any

from accordsync_core import OpId, RecordSnapshot, WireOp

from .storage import StorageSnapshot, StorageTx, StoredMeta


def _copy(o: Any) -> Any:
    return json.loads(json.dumps(o, allow_nan=False))


def _copy_snapshot(s: RecordSnapshot) -> RecordSnapshot:
    return RecordSnapshot.from_json(_copy(s.to_json()))


class MemoryStorage:
    """Keeps everything in memory. `load` returns copies, never live references."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._ops: dict[OpId, WireOp] = {}
        self._snapshots: dict[str, RecordSnapshot] = {}
        self._outbox: dict[OpId, None] = {}  # insertion-ordered set
        self._meta: StoredMeta | None = None

    def load(self) -> StorageSnapshot:
        with self._lock:
            return StorageSnapshot(
                meta=self._meta,
                snapshots=[_copy_snapshot(s) for s in self._snapshots.values()],
                ops=[_copy(o) for o in self._ops.values()],
                outbox=list(self._outbox),
            )

    def commit(self, tx: StorageTx) -> None:
        # Copy first: a value that cannot be serialised fails the commit before any change.
        put_snapshots = [_copy_snapshot(s) for s in tx.put_snapshots]
        put_ops = [_copy(o) for o in tx.put_ops]
        with self._lock:
            if tx.clear_ops:
                self._ops.clear()
                self._snapshots.clear()
            for r in tx.delete_snapshots:
                self._snapshots.pop(r, None)
            for s in put_snapshots:
                self._snapshots[s.record] = s
            for i in tx.delete_ops:
                self._ops.pop(i, None)
            for o in put_ops:
                self._ops[o["op_id"]] = o
            for i in tx.outbox_add:
                self._outbox[i] = None
            for i in tx.outbox_delete:
                self._outbox.pop(i, None)
            if tx.meta is not None:
                self._meta = tx.meta

    def close(self) -> None:
        pass
