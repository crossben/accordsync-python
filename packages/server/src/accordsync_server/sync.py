"""Push and pull, ported line by line from `app/packages/server/src/sync.ts` (ADR-0010)."""

from __future__ import annotations

import hashlib
import random
import threading
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass
from typing import Any

import psycopg
from accordsync_core import (
    AccordError,
    Op,
    RecordSnapshot,
    Replica,
    canonical_json,
    decode_op,
    encode_op,
    parse_op_id,
    record_type,
)
from accordsync_core._text import utf16_key
from psycopg_pool import ConnectionPool

from .define import ScopedRecord, ServerDefinition
from .jsnum import js_safe_integer

FEED_LOCK = 0x4ACC0D
"""Pushes take this advisory lock shared, compaction exclusive (ADR-0010)."""

DAY_MS = 24 * 3_600_000

Conn = psycopg.Connection[tuple[Any, ...]]
Pool = ConnectionPool[Conn]


class BadRequestError(Exception):
    """Answered 400."""


class ForbiddenError(Exception):
    """Answered 403."""


@dataclass(frozen=True, slots=True)
class Caller:
    """Who is calling: verified user, their device, and the scope keys their claims grant."""

    sub: str
    device_id: str
    read: Sequence[str]
    write: Sequence[str]


@dataclass(slots=True)
class SyncContext:
    pool: Pool
    definition: ServerDefinition
    now: Callable[[], int]
    """Physical time in milliseconds."""


_slots_lock = threading.Lock()
_slot_keys: dict[int, tuple[ServerDefinition, threading.BoundedSemaphore]] = {}


def _push_slots(definition: ServerDefinition) -> threading.BoundedSemaphore:
    """At most `max_concurrent_pushes` push transactions at once per process (per definition)."""
    with _slots_lock:
        entry = _slot_keys.get(id(definition))
        if entry is None or entry[0] is not definition:
            entry = (
                definition,
                threading.BoundedSemaphore(definition.limits.max_concurrent_pushes),
            )
            _slot_keys[id(definition)] = entry
        return entry[1]


def device_ttl_ms(definition: ServerDefinition) -> float:
    return definition.compaction.device_ttl_days * DAY_MS


def touch_device(pool: Pool, caller: Caller, ttl_ms: float) -> None:
    """Registers the device to this user on first sight; refuses a device id owned by someone
    else. A device seen again after the retirement TTL is flagged: its next pull starts at zero."""
    with pool.connection() as conn:
        row = conn.execute(
            "insert into devices (device_id, sub, read_keys) values (%s, %s, null)"
            " on conflict (device_id) do update set last_seen = now(),"
            " needs_resync = devices.needs_resync"
            " or devices.last_seen < now() - make_interval(secs => %s)"
            " where devices.sub = %s returning sub",
            (caller.device_id, caller.sub, ttl_ms / 1000, caller.sub),
        ).fetchone()
    if row is None:
        raise ForbiddenError(f"device {caller.device_id} belongs to another user")


def push(ctx: SyncContext, caller: Caller, raw: Sequence[object]) -> dict[str, Any]:
    d = ctx.definition
    max_ops = d.limits.max_push_ops
    if len(raw) > max_ops:
        raise BadRequestError(f"at most {max_ops} ops per push")
    max_skew_ms = d.limits.max_skew_ms

    acked: list[str] = []
    refused: list[dict[str, str]] = []
    by_record: dict[str, list[Op]] = {}

    for item in raw:
        try:
            op = decode_op(item)
        except Exception as e:
            op_id = item.get("op_id") if isinstance(item, dict) else None
            if not isinstance(op_id, str):
                raise BadRequestError(f"malformed op: {e}") from e
            refused.append({"op_id": op_id, "reason": f"malformed op: {e}"})
            continue
        if op.hlc.node != caller.device_id:
            refused.append({"op_id": op.op_id, "reason": f"op belongs to device {op.hlc.node}"})
            continue
        by_record.setdefault(op.record, []).append(op)

    with _push_slots(d), ctx.pool.connection() as conn, conn.transaction():
        _push_tx(ctx, conn, caller, raw, by_record, acked, refused, max_skew_ms)
    return {"acked": acked, "refused": refused}


def _push_tx(
    ctx: SyncContext,
    conn: Conn,
    caller: Caller,
    raw: Sequence[object],
    by_record: Mapping[str, list[Op]],
    acked: list[str],
    refused: list[dict[str, str]],
    max_skew_ms: int,
) -> None:
    d = ctx.definition
    conn.execute("select pg_advisory_xact_lock_shared(%s)", (FEED_LOCK,))
    # Op numbers this device already used (read once: within a batch, ops apply out of order).
    row = conn.execute(
        "select max_op_seq from devices where device_id = %s", (caller.device_id,)
    ).fetchone()
    used_up_to = int(row[0]) if row else 0
    max_applied = 0
    now = ctx.now()
    records = sorted(by_record, key=utf16_key)

    # Create missing record rows, so every record can be locked; a row created here and left
    # unused (every op refused) is removed again below.
    created: set[str] = set()
    if records:
        values = ", ".join(["(%s, '{}'::text[], null)"] * len(records))
        created = {
            r[0]
            for r in conn.execute(
                f"insert into records (record, scopes, state) values {values}"  # noqa: S608 (placeholders only)
                " on conflict (record) do nothing returning record",
                records,
            ).fetchall()
        }
    locked = {
        r[0]: r
        for r in conn.execute(
            "select record, scopes, state from records where record = any(%s::text[])"
            " order by record for update",
            (records or [""],),
        ).fetchall()
    }

    for record in records:
        is_new = record in created
        existing = locked[record]
        if existing[2] is not None:
            replica = Replica(d.schema)
            replica.load_snapshot(RecordSnapshot.from_json(existing[2]))
        else:
            # A new record, or one written before migration 0004: rebuild from the feed.
            replica = load_record(conn, d, record)
        pending = by_record[record]
        ids = [o.op_id for o in pending]
        # Already applied (in the feed) or folded by compaction: acknowledge, never apply twice.
        # An id already in the feed is a retry only if it is the same op.
        stored = {
            r[0]: canonical_json(r[1])
            for r in conn.execute(
                "select op_id, op from feed where op_id = any(%s::text[])", (ids,)
            ).fetchall()
        }
        compacted: dict[str, str | None] = {
            r[0]: r[1]
            for r in conn.execute(
                "select op_id, op_hash from compacted_ops where op_id = any(%s::text[])", (ids,)
            ).fetchall()
        }
        scopes: list[str] | None = None if is_new else list(existing[1])
        changed = False
        rows: list[tuple[str, str | None, str | None, list[str], list[str] | None]] = []

        for op in pending:
            wire = encode_op(op)
            previous = stored.get(op.op_id)
            if previous is not None and previous != canonical_json(wire):
                refused.append(
                    {
                        "op_id": op.op_id,
                        "reason": f"op id already used: {op.op_id} names another op",
                    }
                )
                continue
            folded = compacted.get(op.op_id)
            if isinstance(folded, str) and folded != op_hash(wire):
                refused.append(
                    {
                        "op_id": op.op_id,
                        "reason": f"op id already used: {op.op_id} names another op",
                    }
                )
                continue
            if previous is not None or op.op_id in compacted or replica.has(op.op_id):
                acked.append(op.op_id)  # a retried push: already applied
                continue
            op_seq = parse_op_id(op.op_id).seq
            if op_seq <= used_up_to:
                # Not a retry (that would be in the feed): the device reused an op id.
                refused.append(
                    {
                        "op_id": op.op_id,
                        "reason": "op id already used: this device's ops are numbered"
                        f" above {used_up_to}",
                    }
                )
                continue
            reason = _check(ctx, caller, replica, scopes, op, now, max_skew_ms)
            if reason is not None:
                refused.append({"op_id": op.op_id, "reason": reason})
                continue
            replica.apply(op)
            try:
                nxt = scopes_of(d, replica, record)
            except Exception as e:
                raise RuntimeError(f"scope function failed for {record}: {e}") from e
            # A scope change goes in before the op that caused it: a device the record is entering
            # receives the history first, then this op on top.
            if scopes is None or not same_keys(scopes, nxt):
                rows.append(("scope", None, None, nxt, scopes if scopes is not None else []))
            rows.append(("op", op.op_id, canonical_json(wire), nxt, None))
            scopes = nxt
            changed = True
            acked.append(op.op_id)
            max_applied = max(max_applied, op_seq)
        if changed:
            # One insert per record: rows keep this order (same transaction, increasing seq).
            values = ", ".join(["(%s, %s, %s, %s::jsonb, %s::text[], %s::text[])"] * len(rows))
            params: list[object] = []
            for kind, op_id, op_json, keys, before in rows:
                params += [kind, record, op_id, op_json, keys, before]
            conn.execute(
                "insert into feed (kind, record, op_id, op, scopes, scopes_before)"  # noqa: S608 (placeholders only)
                f" values {values}",
                params,
            )
            conn.execute(
                "update records set scopes = %s::text[], state = %s::jsonb where record = %s",
                (scopes, canonical_json(replica.snapshot_record(record).to_json()), record),
            )
        elif is_new:
            conn.execute("delete from records where record = %s", (record,))

    # The device has its answers for every op below the first one it sent now (clients push their
    # outbox in order): compacted-op entries below that can be forgotten.
    prefix = caller.device_id + ":"
    seqs: list[int] = []
    for item in raw:
        op_id = item.get("op_id") if isinstance(item, dict) else None
        if isinstance(op_id, str) and op_id.startswith(prefix):
            n = js_safe_integer(op_id[len(prefix) :])
            if n is not None:
                seqs.append(n)
    if seqs:
        conn.execute(
            "update devices set push_floor = greatest(push_floor, %s::bigint),"
            " max_op_seq = greatest(max_op_seq, %s::bigint) where device_id = %s",
            (min(seqs), max_applied, caller.device_id),
        )


def _check(
    ctx: SyncContext,
    caller: Caller,
    replica: Replica,
    scopes: list[str] | None,
    op: Op,
    now: int,
    max_skew_ms: int,
) -> str | None:
    """Why `op` must be refused, or None to accept it."""
    try:
        replica.validate(op)
    except AccordError as e:
        return str(e)
    ahead = op.hlc.wall - now
    if ahead > max_skew_ms:
        return f"clock is {ahead} ms ahead of the server (limit {max_skew_ms} ms)"
    # An existing record: the caller must be allowed to write where it is now. A new record: where
    # the write puts it.
    where = scopes
    if where is None:
        fresh = Replica(ctx.definition.schema)
        fresh.apply(op)
        try:
            where = scopes_of(ctx.definition, fresh, op.record)
        except Exception as e:
            return f"scope function failed: {e}"
    if not overlaps(where, caller.write):
        return f"out of scope: you may not write {op.record}"
    return None


def pull(ctx: SyncContext, caller: Caller, cursor: int, requested: int) -> dict[str, Any]:
    limit = max(1, min(requested, ctx.definition.limits.max_pull_limit))
    read = normalize(caller.read)
    # Two pulls from one device at once can collide on its `devices` row under repeatable read
    # (40001). The pull only reads the feed, so running it again is always safe.
    attempt = 1
    while True:
        try:
            return _pull_once(ctx, caller, cursor, limit, read)
        except psycopg.Error as e:
            if e.sqlstate != "40001" or attempt >= 20:
                raise
            time.sleep(random.random() * 10 * attempt / 1000)  # noqa: S311 (jitter)
            attempt += 1


_VISIBLE = (
    "((kind in ('op', 'snapshot') and scopes && %(read)s::text[])"
    " or (kind = 'scope' and (scopes_before && %(read)s::text[]) <> (scopes && %(read)s::text[])))"
)


def _pull_once(
    ctx: SyncContext, caller: Caller, cursor: int, limit: int, read: list[str]
) -> dict[str, Any]:
    with ctx.pool.connection() as conn, conn.transaction():
        conn.execute("set transaction isolation level repeatable read")
        device = conn.execute(
            "select read_keys, needs_resync, max_op_seq from devices where device_id = %s",
            (caller.device_id,),
        ).fetchone()
        if device is None:
            raise RuntimeError(f"no device row for {caller.device_id}")
        if cursor > 0 and device[1]:
            return {"resync_required": True}
        before: list[str] = list(device[0] or [])
        keys_changed = cursor > 0 and not same_keys(before, read)
        delta: tuple[list[tuple[str, Any]], list[str]] | None = None
        if keys_changed:
            delta = _scope_delta(conn, before, read, ctx.definition.limits.max_scope_delta)
            if delta is None:
                return {"resync_required": True}
            conn.execute(
                "update devices set read_keys = %s::text[] where device_id = %s",
                (read, caller.device_id),
            )
        # The device has applied everything up to `cursor`: compaction may fold ops below it.
        if cursor == 0:
            conn.execute(
                "update devices set read_keys = %s::text[], needs_resync = false, cursor = 0"
                " where device_id = %s",
                (read, caller.device_id),
            )
        else:
            conn.execute(
                "update devices set cursor = greatest(cursor, %s::bigint) where device_id = %s",
                (cursor, caller.device_id),
            )

        # Rows from transactions older than every transaction still running are final.
        h = conn.execute("select accord_horizon()").fetchone()
        assert h is not None
        horizon = int(h[0])
        cols = "select seq, pos, kind, record, op, scopes, scopes_before from feed"
        fetched = conn.execute(
            f"{cols} where pos > %(cursor)s and pos < %(horizon)s and {_VISIBLE}"
            " order by pos, seq limit %(n)s",
            {"cursor": cursor, "horizon": horizon, "read": read, "n": limit + 1},
        ).fetchall()

        # A page ends on a transaction boundary: the cursor is a transaction position. A
        # transaction bigger than a page is sent whole.
        rows = fetched
        full = False
        if len(fetched) > limit:
            full = True
            last_pos = fetched[limit][1]
            rows = [r for r in fetched[:limit] if r[1] != last_pos]
            if not rows:
                rows = conn.execute(
                    f"{cols} where pos = %(pos)s and {_VISIBLE} order by seq",
                    {"pos": last_pos, "read": read},
                ).fetchall()

        items: list[dict[str, Any]] = []
        sent: set[str] = set()

        def send(kind: str, op: Any) -> None:
            if kind == "snapshot":
                items.append({"type": "snapshot", "snapshot": op})
                return
            if op["op_id"] in sent:
                return
            sent.add(op["op_id"])
            items.append({"type": "op", "op": op})

        if delta is not None:
            for kind, op in delta[0]:
                send(kind, op)
            for record in delta[1]:
                items.append({"type": "exit", "record": record})
        for seq, pos, kind, record, op, scopes, _before in rows:
            if kind in ("op", "snapshot"):
                send(kind, op)
                continue
            if overlaps(scopes, read):
                # The record entered the caller's scope: send its whole history up to this point.
                for k, o in conn.execute(
                    "select kind, op from feed where record = %s and kind in ('op', 'snapshot')"
                    " and (pos, seq) < (%s::bigint, %s::bigint) order by pos, seq",
                    (record, pos, seq),
                ).fetchall():
                    send(k, o)
            else:
                items.append({"type": "exit", "record": record})

        nxt = int(rows[-1][1]) if full else horizon - 1
        return {
            "items": items,
            "cursor": max(cursor, nxt),
            "has_more": full,
            "device_seq": int(device[2]),
        }


def _scope_delta(
    conn: Conn, before: Sequence[str], after: Sequence[str], max_records: int
) -> tuple[list[tuple[str, Any]], list[str]] | None:
    """History of every record now visible that was not, and an exit for every record no longer
    visible; None when more than `max_records` change (a full resync is cheaper then)."""
    was, now = list(before), list(after)
    entering = conn.execute(
        "select record from records where scopes && %s::text[] and not (scopes && %s::text[])"
        " limit %s",
        (now, was, max_records + 1),
    ).fetchall()
    leaving = conn.execute(
        "select record from records where scopes && %s::text[] and not (scopes && %s::text[])"
        " limit %s",
        (was, now, max_records + 1),
    ).fetchall()
    if len(entering) + len(leaving) > max_records:
        return None
    history: list[tuple[str, Any]] = []
    if entering:
        history = [
            (r[0], r[1])
            for r in conn.execute(
                "select kind, op from feed where record = any(%s::text[])"
                " and kind in ('op', 'snapshot') order by record, pos, seq",
                ([r[0] for r in entering],),
            ).fetchall()
        ]
    return history, [r[0] for r in leaving]


def load_record(conn: Conn, definition: ServerDefinition, record: str) -> Replica:
    """A record's state on the server: its latest snapshot, then the ops after it."""
    replica = Replica(definition.schema)
    for kind, op in conn.execute(
        "select kind, op from feed where record = %s and kind in ('op', 'snapshot')"
        " order by pos, seq",
        (record,),
    ).fetchall():
        if kind == "snapshot":
            replica.load_snapshot(RecordSnapshot.from_json(op))
        else:
            replica.apply(decode_op(op))
    return replica


def scopes_of(definition: ServerDefinition, replica: Replica, record: str) -> list[str]:
    type_ = record_type(record)
    fn = definition.scopes.get(type_)
    if fn is None:
        raise ValueError(f"no scope function for {type_}")
    return normalize(fn(ScopedRecord(record, replica.read(record) or {})))


def normalize(keys: Iterable[str]) -> list[str]:
    """Distinct keys in JavaScript's default sort order (UTF-16 code units)."""
    return sorted(set(keys), key=utf16_key)


def overlaps(a: Iterable[str], b: Iterable[str]) -> bool:
    s = set(b)
    return any(k in s for k in a)


def same_keys(a: Iterable[str], b: Iterable[str]) -> bool:
    return normalize(a) == normalize(b)


def op_hash(op: Mapping[str, Any]) -> str:
    """The hash a compacted op is remembered by: SHA-256, lowercase hex, of the UTF-8 canonical
    JSON of its wire form. Every server computes it the same way (it is stored in the database).
    Canonical JSON escapes lone surrogates, so the text always encodes."""
    return hashlib.sha256(canonical_json(op).encode("utf-8")).hexdigest()
