"""The Accord client: a device that writes locally first and syncs with a server.

Port of `@accordsync/client` (client.ts), following the Dart port. Synchronous (ADR-Y01): writes
and `sync()` run on the caller's thread; `start()` syncs on a daemon thread. See ADR-Y05 for the
threading rules.
"""

from __future__ import annotations

import contextlib
import logging
import random as _random
import secrets
import threading
import time
from collections.abc import Callable, Sequence
from concurrent.futures import Future
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias, TypeVar

from accordsync_core import (
    MAX_SAFE_INTEGER,
    AddOp,
    AssignOp,
    Hlc,
    IncOp,
    JsonValue,
    LocalWriter,
    Op,
    OpId,
    RecordSnapshot,
    RemoveOp,
    Schema,
    SetElement,
    decode_op,
    encode_op,
    parse_op_id,
)

from .storage import StorageAdapter, StorageTx, StoredMeta
from .transport import ExitItem, OpItem, PullItem, PullPage, SnapshotItem, Transport

log = logging.getLogger("accordsync")

Event: TypeAlias = Literal["change", "refused", "synced", "resync", "error"]
EVENTS: tuple[Event, ...] = ("change", "refused", "synced", "resync", "error")
Listener: TypeAlias = Callable[[Any], None]

_O = TypeVar("_O", AssignOp, IncOp, AddOp, RemoveOp)


@dataclass(frozen=True, slots=True)
class Refusal:
    """A local write the server refused. It has already been rolled back on this device."""

    op_id: OpId
    record: str
    field: str
    reason: str


@dataclass(frozen=True, slots=True)
class ConflictValue:
    """One of the values a conflicted field holds, and the op that wrote it."""

    value: JsonValue
    op_id: OpId


@dataclass(frozen=True, slots=True)
class ConflictInfo:
    """A `conflict()` field holding more than one value. `values` are sorted by op id."""

    record: str
    field: str
    values: tuple[ConflictValue, ...]


@dataclass(frozen=True, slots=True)
class SyncStatus:
    pending: int
    cursor: int
    last_sync_at: int | None
    last_error: BaseException | None


def random_device_id() -> str:
    """A random device id from a secure source. Device ids must never collide."""
    return "d" + secrets.token_hex(16)


def _now_ms() -> int:
    return time.time_ns() // 1_000_000


class AccordClient:
    """An Accord device. Writes apply locally at once and are saved to storage; sync pushes them
    and pulls everyone else's, in the background (`start()`) or on demand (`sync()`).

    Events (`on(event, listener)`), with the listener's argument:

    - ``"change"``: ``list[str]``, records whose local state changed;
    - ``"refused"``: `Refusal`, a local write the server refused, already rolled back;
    - ``"synced"``: ``int``, the cursor after a full round;
    - ``"resync"``: ``None``, the server asked for a resync and local data was reloaded;
    - ``"error"``: the exception of a failed background round (retried with backoff).

    Listeners run on the thread that caused the event, without the client's lock held. An
    exception in a listener is logged and ignored.
    """

    def __init__(
        self,
        *,
        schema: Schema,
        storage: StorageAdapter,
        transport: Transport,
        device_id: str,
        meta: StoredMeta | None,
        now: Callable[[], int],
        random: Callable[[], float],
        push_batch: int,
        pull_limit: int,
        sync_interval: float,
        min_backoff: float,
        max_backoff: float,
    ) -> None:
        """Use `AccordClient.open`."""
        self.device_id = device_id
        self._schema = schema
        self._storage = storage
        self._transport = transport
        self._now = now
        self._random = random
        self._push_batch = push_batch
        self._pull_limit = pull_limit
        self._sync_interval = sync_interval
        self._min_backoff = min_backoff
        self._max_backoff = max_backoff

        # All state below is guarded by `_lock`, never held during network I/O.
        self._lock = threading.RLock()
        self._cursor = meta.cursor if meta else 0
        self._writer = self._new_writer((Hlc.decode(meta.hlc), meta.seq) if meta else None)
        self._outbox: dict[OpId, Op] = {}  # unacknowledged local ops, in write order
        self._last_sync_at: int | None = None
        self._last_error: BaseException | None = None
        self._failures = 0

        # One sync round at a time; concurrent callers share it.
        self._round_guard = threading.Lock()
        self._round: Future[None] | None = None
        self._round_thread: int | None = None
        # A write landed during a round, after its push: the loop runs another one soon after.
        self._again = False

        # Background sync.
        self._cond = threading.Condition()
        self._running = False
        self._generation = 0
        self._next_at = 0.0
        self._thread: threading.Thread | None = None

        self._listeners: dict[str, list[Listener]] = {e: [] for e in EVENTS}
        self._listeners_lock = threading.Lock()

    @classmethod
    def open(
        cls,
        *,
        schema: Schema,
        storage: StorageAdapter,
        transport: Transport,
        device_id: str | None = None,
        now: Callable[[], int] | None = None,
        random: Callable[[], float] | None = None,
        push_batch: int = 200,
        pull_limit: int = 500,
        sync_interval: float = 30.0,
        min_backoff: float = 1.0,
        max_backoff: float = 60.0,
    ) -> AccordClient:
        """Opens the device: loads its stored ops, outbox and cursor.

        `device_id` is used only the first time; afterwards the stored id is kept. Generated when
        absent. `now` returns physical time in ms. `push_batch` is the ops per push request and
        `pull_limit` the items per pull page. Background sync pauses `sync_interval` seconds
        between successful rounds and backs off between `min_backoff` and `max_backoff` seconds on
        errors (with jitter from `random`).
        """
        snap = storage.load()
        meta = snap.meta
        did = meta.device_id if meta else (device_id or random_device_id())
        client = cls(
            schema=schema,
            storage=storage,
            transport=transport,
            device_id=did,
            meta=meta,
            now=now or _now_ms,
            random=random or _random.random,
            push_batch=push_batch,
            pull_limit=pull_limit,
            sync_interval=sync_interval,
            min_backoff=min_backoff,
            max_backoff=max_backoff,
        )
        for base in snap.snapshots:
            client._writer.replica.load_snapshot(base)
        by_id: dict[OpId, Op] = {}
        for raw in snap.ops:
            op = decode_op(raw)
            by_id[op.op_id] = op
            client._writer.receive(op)
        for oid in sorted(snap.outbox, key=lambda i: parse_op_id(i).seq):
            found = by_id.get(oid)
            if found is not None:
                client._outbox[oid] = found
        if meta is None:
            client._persist(StorageTx())
        return client

    # ── events ────────────────────────────────────────────────────────────

    def on(self, event: Event, listener: Listener) -> Callable[[], None]:
        """Subscribes to an event; returns a function that unsubscribes."""
        if event not in self._listeners:
            raise ValueError(f"unknown event {event!r}; expected one of {EVENTS}")
        with self._listeners_lock:
            self._listeners[event].append(listener)

        def off() -> None:
            with self._listeners_lock:
                if listener in self._listeners[event]:
                    self._listeners[event].remove(listener)

        return off

    def _emit(self, event: Event, payload: Any) -> None:
        with self._listeners_lock:
            listeners = list(self._listeners[event])
        for fn in listeners:
            try:
                fn(payload)
            except Exception:
                log.exception('accord: a "%s" listener raised', event)

    # ── reading ───────────────────────────────────────────────────────────

    def read(self, record: str) -> dict[str, object] | None:
        """A record's fields, or None if this device has never seen it. Fields with no value are
        left out."""
        with self._lock:
            return self._writer.replica.read(record)

    def records(self, type: str | None = None) -> list[str]:
        with self._lock:
            everything = self._writer.replica.records()
        return everything if type is None else [r for r in everything if r.startswith(f"{type}:")]

    def conflicts(self) -> list[ConflictInfo]:
        """Every `conflict()` field holding more than one value, with the values."""
        with self._lock:
            out: list[ConflictInfo] = []
            for ref in self._writer.replica.conflicts():
                fields = self._writer.replica.read(ref.record) or {}
                shown: Any = fields[ref.field]
                values = tuple(ConflictValue(v["value"], v["opId"]) for v in shown["conflicted"])
                out.append(ConflictInfo(ref.record, ref.field, values))
            return out

    def status(self) -> SyncStatus:
        with self._lock:
            return SyncStatus(
                pending=len(self._outbox),
                cursor=self._cursor,
                last_sync_at=self._last_sync_at,
                last_error=self._last_error,
            )

    # ── writing (local-first) ─────────────────────────────────────────────

    def assign(self, record: str, field: str, value: JsonValue) -> AssignOp:
        """Sets a `lww` or `conflict` field. Returns once the write is saved on this device."""
        return self._write(lambda w: w.assign(record, field, value))

    def resolve(self, record: str, field: str, value: JsonValue) -> AssignOp:
        """Resolves a conflicted field: writes `value`, superseding every value currently shown."""
        return self.assign(record, field, value)

    def inc(self, record: str, field: str, by: int) -> IncOp:
        return self._write(lambda w: w.inc(record, field, by))

    def add(self, record: str, field: str, element: SetElement) -> AddOp:
        return self._write(lambda w: w.add(record, field, element))

    def remove(self, record: str, field: str, element: SetElement) -> RemoveOp:
        return self._write(lambda w: w.remove(record, field, element))

    def _write(self, make: Callable[[LocalWriter], _O]) -> _O:
        error: BaseException | None = None
        with self._lock:
            op = make(self._writer)
            self._outbox[op.op_id] = op
            try:
                self._persist(StorageTx(put_ops=[encode_op(op)], outbox_add=[op.op_id]))
            except BaseException as e:  # reported after the change event, like client.ts
                error = e
        self._emit("change", [op.record])
        if error is not None:
            raise error
        self._soon()
        return op

    # ── sync ──────────────────────────────────────────────────────────────

    def sync(self) -> None:
        """One full round: push the outbox, then pull every page. A call made while a round is
        running waits for that round and shares its outcome."""
        with self._round_guard:
            running = self._round
            if running is None:
                mine: Future[None] = Future()
                self._round = mine
                self._round_thread = threading.get_ident()
        if running is not None:
            if self._round_thread == threading.get_ident():
                raise RuntimeError("sync() called from inside its own round (from a listener?)")
            running.result()
            return
        try:
            self._run_round()
        except BaseException as e:
            self._finish_round(mine, e)
            raise
        self._finish_round(mine, None)

    def _finish_round(self, fut: Future[None], error: BaseException | None) -> None:
        with self._round_guard:
            self._round = None
            self._round_thread = None
        if error is None:
            fut.set_result(None)
        else:
            fut.set_exception(error)

    def start(self) -> None:
        """Syncs in the background, on a daemon thread: soon after each write, every
        `sync_interval`, and with backoff on errors."""
        with self._cond:
            if self._running:
                return
            self._running = True
            self._generation += 1
            self._next_at = time.monotonic()
            self._thread = threading.Thread(
                target=self._loop, args=(self._generation,), name="accord-sync", daemon=True
            )
            self._thread.start()

    def stop(self) -> None:
        """Stops background sync. A round in flight finishes."""
        with self._cond:
            self._running = False
            self._cond.notify_all()

    def flush(self) -> None:
        """Waits for a local save in progress. Saves are synchronous, so this returns at once
        unless another thread is writing."""
        with self._lock:
            pass

    def close(self) -> None:
        """Stops background sync, waits for a round in flight, and closes the storage."""
        self.stop()
        with self._round_guard:
            running, owner = self._round, self._round_thread
        if running is not None and owner != threading.get_ident():
            # The round's error, if any, was reported to its caller.
            with contextlib.suppress(BaseException):
                running.result()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join()
        self.flush()
        self._storage.close()

    def _run_round(self) -> None:
        self._push_all()
        while True:
            with self._lock:
                cursor = self._cursor
            page = self._transport.pull(self.device_id, cursor, self._pull_limit)
            if not isinstance(page, PullPage):
                self._push_all()
                self._resync()
                continue
            with self._lock:
                # The server's count of this device's ops: never reuse an op id, even after lost
                # storage.
                if page.device_seq is not None:
                    self._writer.advance_seq(page.device_seq)
                changed = self._apply_page(page.items, page.cursor)
            if changed:
                self._emit("change", changed)
            if not page.has_more:
                break
        with self._lock:
            self._last_sync_at = self._now()
            self._last_error = None
            self._failures = 0
            cursor = self._cursor
        self._emit("synced", cursor)

    def _push_all(self) -> None:
        while True:
            with self._lock:
                if not self._outbox:
                    return
                batch = [encode_op(o) for o in list(self._outbox.values())[: self._push_batch]]
            res = self._transport.push(self.device_id, batch)
            if not res.acked and not res.refused:
                raise RuntimeError("server neither acknowledged nor refused a non-empty push")
            refusals: list[Refusal] = []
            with self._lock:
                for oid in res.acked:
                    self._outbox.pop(oid, None)
                for r in res.refused:
                    op = self._outbox.pop(r.op_id, None)
                    if op is not None:
                        refusals.append(Refusal(op.op_id, op.record, op.field, r.reason))
                if refusals:
                    self._writer.discard(r.op_id for r in refusals)
                self._persist(
                    StorageTx(
                        outbox_delete=[*res.acked, *(r.op_id for r in res.refused)],
                        delete_ops=[r.op_id for r in refusals],
                    )
                )
            if refusals:
                self._emit("change", list(dict.fromkeys(r.record for r in refusals)))
            for refusal in refusals:
                self._emit("refused", refusal)

    def _apply_page(self, items: Sequence[PullItem], cursor: int) -> list[str]:
        """Applies one pull page and saves it. Caller holds the lock. Returns changed records."""
        put: dict[OpId, Op] = {}
        forgotten: list[OpId] = []
        snapshots: list[RecordSnapshot] = []
        drop_snapshots: list[str] = []
        changed: dict[str, None] = {}
        pending = set(self._outbox)

        def not_pending(record: str) -> list[OpId]:
            return [
                o.op_id
                for o in self._writer.replica.ops()
                if o.record == record and o.op_id not in pending
            ]

        for item in items:
            if isinstance(item, OpItem):
                op = decode_op(item.op)
                if self._writer.receive(op) == "applied":
                    put[op.op_id] = op
                    changed[op.record] = None
            elif isinstance(item, SnapshotItem):
                # A compacted record: its snapshot replaces the ops it folded; our unpushed edits
                # stay on top. The server sends a snapshot before any later op of that record.
                snap = item.snapshot
                for oid in not_pending(snap.record):
                    put.pop(oid, None)
                    forgotten.append(oid)
                self._writer.replica.load_snapshot(snap, pending)
                snapshots.append(snap)
                changed[snap.record] = None
            elif isinstance(item, ExitItem):
                # The record left our scope: forget it, except our own unpushed edits, which will
                # be pushed, refused and rolled back like any other refused write.
                ids = not_pending(item.record)
                for oid in ids:
                    put.pop(oid, None)
                self._writer.forget(item.record, pending)
                forgotten.extend(ids)
                drop_snapshots.append(item.record)
                changed[item.record] = None
        self._cursor = max(self._cursor, cursor)
        snapshotted = {s.record for s in snapshots}
        self._persist(
            StorageTx(
                delete_ops=forgotten,
                delete_snapshots=[r for r in drop_snapshots if r not in snapshotted],
                put_snapshots=[s for s in snapshots if s.record not in drop_snapshots],
                put_ops=[encode_op(o) for o in put.values()],
            )
        )
        return list(changed)

    def _resync(self) -> None:
        """Read scopes changed: keep only unpushed local ops and pull everything again from 0."""
        with self._lock:
            keep = list(self._outbox.values())
            before = self._writer.replica.records()
            self._writer = self._new_writer((self._writer.clock, self._writer.seq))
            for op in keep:
                self._writer.receive(op)
            self._cursor = 0
            self._persist(StorageTx(clear_ops=True, put_ops=[encode_op(o) for o in keep]))
        self._emit("resync", None)
        self._emit("change", before)

    # ── background sync ───────────────────────────────────────────────────

    def _schedule(self, delay: float) -> None:
        with self._cond:
            self._next_at = time.monotonic() + delay
            self._cond.notify_all()

    def _soon(self) -> None:
        """After a write, sync shortly (writes in a burst share one round)."""
        with self._lock:
            failures = self._failures
        with self._cond:
            running = self._running
        if running and failures == 0:
            with self._round_guard:
                if self._round is not None:
                    self._again = True
            self._schedule(0.05)

    def _loop(self, generation: int) -> None:
        while True:
            with self._cond:
                while True:
                    if not self._running or self._generation != generation:
                        return
                    wait = self._next_at - time.monotonic()
                    if wait <= 0:
                        break
                    self._cond.wait(wait)
                self._next_at = float("inf")
            try:
                self.sync()
            except Exception as error:
                with self._lock:
                    self._failures += 1
                    self._last_error = error
                    failures = self._failures
                self._emit("error", error)
                base = min(self._max_backoff, self._min_backoff * 2 ** (failures - 1))
                # Jitter: devices don't retry in lockstep.
                self._schedule(base * (0.5 + self._random() / 2))
            else:
                # A write during the round (ours or a manual sync() we joined) must not wait
                # a full interval.
                with self._round_guard:
                    again, self._again = self._again, False
                self._schedule(0.05 if again else self._sync_interval)

    # ── internals ─────────────────────────────────────────────────────────

    def _new_writer(self, resume: tuple[Hlc, int] | None) -> LocalWriter:
        return LocalWriter(
            self._schema,
            self.device_id,
            self._now,
            # The server already refused ops with absurd clocks; a device with a wrong clock of
            # its own must still accept everything the server sends.
            max_skew_ms=MAX_SAFE_INTEGER,
            resume=resume,
        )

    def _persist(self, tx: StorageTx) -> None:
        """Commits `tx` with the current meta. Caller holds the lock (or is `open`)."""
        meta = StoredMeta(
            device_id=self.device_id,
            cursor=self._cursor,
            hlc=self._writer.clock.encode(),
            seq=self._writer.seq,
        )
        self._storage.commit(tx.with_meta(meta))
