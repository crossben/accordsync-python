"""An in-memory Accord server, enough to exercise every client path without PostgreSQL (port of
the Dart client's test/support/fake_server.dart).

It follows the protocol rules a client depends on (docs/protocol.md): pushes applied in order and
idempotent, refusals (out of scope, op id already used, schema), `device_seq`, paged pulls with
`has_more`, exits when a record leaves the reader's scope and its whole history when it enters,
snapshots after compaction (folded ops leave empty slots, so stored cursors keep their meaning),
and resync on demand. The real server is checked in Y3.
"""

from __future__ import annotations

import json
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from typing import Any

from accordsync import (
    AccordError,
    ExitItem,
    HttpError,
    OpItem,
    PullItem,
    PullPage,
    PullResult,
    PushResult,
    RecordSnapshot,
    RefusedOp,
    ResyncRequired,
    Schema,
    SnapshotItem,
    WireOp,
)
from accordsync_core import Replica, canonical_json, decode_op, parse_op_id


@dataclass(frozen=True)
class Access:
    read: list[str]
    write: list[str]


FeedEntry = WireOp | RecordSnapshot | None


@dataclass
class FakeServer:
    schema: Schema
    scopes: Callable[[str, dict[str, object]], list[str]]
    """A record's scope keys, from its current fields."""
    access: Callable[[str], Access]
    """A user's keys. Read on every request, like JWT claims."""

    fail_next: int = 0
    """Fails the next n requests, as a dropped connection would."""
    requests: int = 0

    _feed: list[FeedEntry] = field(default_factory=list)
    _applied: dict[str, str] = field(default_factory=dict)
    _device_user: dict[str, str] = field(default_factory=dict)
    _device_seq: dict[str, int] = field(default_factory=dict)
    _sent: dict[str, set[str]] = field(default_factory=dict)
    _resync: set[str] = field(default_factory=set)
    _lock: threading.RLock = field(default_factory=threading.RLock)

    def __post_init__(self) -> None:
        self._state = Replica(self.schema)

    def transport_for(self, user: str) -> FakeTransport:
        return FakeTransport(self, user)

    def require_resync(self, device_id: str) -> None:
        """The next pull of `device_id` answers `resync_required`."""
        self._resync.add(device_id)

    def compact(self, record: str) -> None:
        """Folds a record's history into a snapshot (ADR-0008): its ops leave the feed, and a
        snapshot entry is appended."""
        with self._lock:
            for i, e in enumerate(self._feed):
                if _record_of(e) == record:
                    self._feed[i] = None
            self._feed.append(self._state.snapshot_record(record))

    def _check(self, user: str, device_id: str) -> None:
        self.requests += 1
        if self.fail_next > 0:
            self.fail_next -= 1
            raise HttpError(503, "network down")
        owner = self._device_user.setdefault(device_id, user)
        if owner != user:
            raise HttpError(403, f"device {device_id} belongs to another user")

    def _keys(self, r: Replica, record: str) -> list[str]:
        fields = r.read(record)
        return [] if fields is None else self.scopes(record, fields)

    def push(self, user: str, device_id: str, ops: Sequence[WireOp]) -> PushResult:
        with self._lock:
            self._check(user, device_id)
            acked: list[str] = []
            refused: list[RefusedOp] = []
            for raw in ops:
                oid = str(raw["op_id"])
                canonical = canonical_json(raw)
                seen = self._applied.get(oid)
                if seen is not None:
                    if seen == canonical:
                        acked.append(oid)
                    else:
                        refused.append(RefusedOp(oid, "op id already used"))
                    continue
                try:
                    op = decode_op(raw)
                    if parse_op_id(oid).device != device_id:
                        raise AccordError("belongs to another device")
                    self._state.validate(op)
                except AccordError as e:
                    refused.append(RefusedOp(oid, str(e)))
                    continue
                write = self.access(user).write
                existing = self._state.read(op.record) is not None
                after = self._state.without(())
                after.apply(op)
                keys = self._keys(self._state if existing else after, op.record)
                if not _overlap(keys, write):
                    refused.append(RefusedOp(oid, f"out of scope: you may not write {op.record}"))
                    continue
                self._state = after
                self._applied[oid] = canonical
                self._feed.append(json.loads(json.dumps(raw)))
                seq = parse_op_id(oid).seq
                if seq > self._device_seq.get(device_id, 0):
                    self._device_seq[device_id] = seq
                acked.append(oid)
            return PushResult(acked, refused)

    def pull(self, user: str, device_id: str, cursor: int, limit: int) -> PullResult:
        with self._lock:
            self._check(user, device_id)
            if device_id in self._resync:
                self._resync.discard(device_id)
                self._sent[device_id] = set()
                return ResyncRequired()
            if cursor == 0:
                self._sent[device_id] = set()
            sent = self._sent.setdefault(device_id, set())
            read = self.access(user).read

            def visible(record: str) -> bool:
                return _overlap(self._keys(self._state, record), read)

            items: list[PullItem] = []
            # Scope changes since the last pull: exits, then the whole history of records that
            # entered.
            for r in sorted(sent):
                if not visible(r):
                    sent.discard(r)
                    items.append(ExitItem(r))
            for r in self._state.records():
                if visible(r) and r not in sent and self._record_before(r, cursor):
                    sent.add(r)
                    items.extend(self._history(r, cursor))
            pos = cursor
            while pos < len(self._feed) and len(items) < limit:
                e = self._feed[pos]
                pos += 1
                record = _record_of(e)
                if record is None or not visible(record):
                    continue
                sent.add(record)
                items.append(SnapshotItem(e) if isinstance(e, RecordSnapshot) else OpItem(_wire(e)))
            return PullPage(
                items=items,
                cursor=pos,
                has_more=pos < len(self._feed),
                device_seq=self._device_seq.get(device_id),
            )

    def _record_before(self, record: str, cursor: int) -> bool:
        """Whether the record has feed entries this device already skipped (it entered late)."""
        return any(_record_of(e) == record for e in self._feed[:cursor])

    def _history(self, record: str, cursor: int) -> Iterator[PullItem]:
        for e in self._feed[:cursor]:
            if isinstance(e, RecordSnapshot) and e.record == record:
                yield SnapshotItem(e)
            elif isinstance(e, dict) and e["record"] == record:
                yield OpItem(_wire(e))


def _overlap(a: list[str], b: list[str]) -> bool:
    return any(k in b for k in a)


def _record_of(e: FeedEntry) -> str | None:
    if isinstance(e, RecordSnapshot):
        return e.record
    if isinstance(e, dict):
        return str(e["record"])
    return None


def _wire(e: FeedEntry) -> WireOp:
    assert isinstance(e, dict)
    return e


class FakeTransport:
    """Through JSON both ways, like the network."""

    def __init__(self, server: FakeServer, user: str) -> None:
        self.server = server
        self.user = user

    def push(self, device_id: str, ops: Sequence[WireOp]) -> PushResult:
        wire: list[dict[str, Any]] = json.loads(json.dumps(list(ops)))
        return self.server.push(self.user, device_id, wire)

    def pull(self, device_id: str, cursor: int, limit: int) -> PullResult:
        page = self.server.pull(self.user, device_id, cursor, limit)
        if isinstance(page, PullPage):
            # Copies, so the client never shares objects with the server.
            items: list[PullItem] = []
            for i in page.items:
                if isinstance(i, OpItem):
                    items.append(OpItem(json.loads(json.dumps(i.op))))
                elif isinstance(i, SnapshotItem):
                    snap = json.loads(json.dumps(i.snapshot.to_json()))
                    items.append(SnapshotItem(RecordSnapshot.from_json(snap)))
                else:
                    items.append(i)
            return PullPage(items, page.cursor, page.has_more, page.device_seq)
        return page
