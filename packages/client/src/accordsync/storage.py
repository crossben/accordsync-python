"""What a device stores, and the adapter contract every storage backend implements."""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any, Protocol

from accordsync_core import OpId, RecordSnapshot, WireOp


@dataclass(frozen=True, slots=True)
class StoredMeta:
    """What a device must remember between launches."""

    device_id: str
    cursor: int
    """Pull cursor: server feed position already applied."""
    hlc: str
    """Last clock (encoded) and op sequence number, so op ids are never reused after a restart."""
    seq: int

    def to_json(self) -> dict[str, Any]:
        """The TypeScript shape (`deviceId`, `cursor`, `hlc`, `seq`), as stored in `accord_meta`."""
        return {"deviceId": self.device_id, "cursor": self.cursor, "hlc": self.hlc, "seq": self.seq}

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> StoredMeta:
        return StoredMeta(
            device_id=str(data["deviceId"]),
            cursor=int(data["cursor"]),
            hlc=str(data["hlc"]),
            seq=int(data["seq"]),
        )


@dataclass(frozen=True, slots=True)
class StorageSnapshot:
    """Everything a device has stored."""

    meta: StoredMeta | None = None
    snapshots: Sequence[RecordSnapshot] = ()
    """Compacted records (ADR-0008). Loaded before ops."""
    ops: Sequence[WireOp] = ()
    """Every op the device holds (its own and received), in the wire format."""
    outbox: Sequence[OpId] = ()
    """Ids of local ops not yet acknowledged by the server."""


@dataclass(frozen=True, slots=True)
class StorageTx:
    """One atomic change. Applied in this order: clear, delete, put, outbox changes, meta."""

    clear_ops: bool = False
    """Removes every op and snapshot."""
    delete_snapshots: Sequence[str] = ()
    put_snapshots: Sequence[RecordSnapshot] = ()
    delete_ops: Sequence[OpId] = ()
    put_ops: Sequence[WireOp] = ()
    outbox_add: Sequence[OpId] = ()
    outbox_delete: Sequence[OpId] = ()
    meta: StoredMeta | None = field(default=None)

    def with_meta(self, meta: StoredMeta) -> StorageTx:
        return dataclasses.replace(self, meta=meta)


class StorageAdapter(Protocol):
    """Durable storage for a device. `commit` must be atomic: after a crash, either all of a
    transaction is visible or none of it. Implementations must be safe to call from several
    threads."""

    def load(self) -> StorageSnapshot: ...

    def commit(self, tx: StorageTx) -> None: ...

    def close(self) -> None: ...
