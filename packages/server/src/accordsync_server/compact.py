"""Log compaction, from `app/packages/server/src/compact.ts` (ADR-0005, ADR-0008, ADR-0010)."""

from __future__ import annotations

from typing import Any, TypedDict

from accordsync_core import canonical_json, encode_op, parse_op_id

from .define import ServerDefinition
from .sync import FEED_LOCK, Pool, device_ttl_ms, load_record, op_hash


class CompactionResult(TypedDict):
    watermark: int
    """Feed position every live device has applied: only ops at or below it were folded."""
    records: int
    opsFolded: int
    tombstonesPruned: int


def compact(pool: Pool, definition: ServerDefinition) -> CompactionResult:
    """Folds the history of records every live device already has into one snapshot per record.

    The snapshot takes the position of the last op it replaces, so live devices (past it) never
    receive it; devices starting from zero and records entering a scope get it instead.
    """
    min_ops = definition.compaction.min_ops
    ttl_secs = device_ttl_ms(definition) / 1000
    with pool.connection() as conn, conn.transaction():
        # Exclusive: waits for running pushes (they hold the lock shared) and holds new ones back.
        conn.execute("select pg_advisory_xact_lock(%s)", (FEED_LOCK,))
        conn.execute("select set_config('accord.compaction', 'on', true)")
        w = conn.execute(
            "select coalesce((select min(cursor) from devices"
            " where last_seen > now() - make_interval(secs => %s)), accord_horizon() - 1) as w",
            (ttl_secs,),
        ).fetchone()
        assert w is not None
        watermark = int(w[0])
        candidates: list[tuple[Any, ...]] = conn.execute(
            "select record, max(pos) as last from feed where kind in ('op', 'snapshot')"
            " group by record having max(pos) <= %s"
            " and count(*) filter (where kind = 'op') >= %s",
            (watermark, min_ops),
        ).fetchall()

        ops_folded = 0
        for record, last in candidates:
            replica = load_record(conn, definition, record)
            ops = replica.ops()
            row = conn.execute("select scopes from records where record = %s", (record,)).fetchone()
            if row is None:
                raise RuntimeError(f"no records row for {record}")
            if ops:
                values = ", ".join(["(%s, %s, %s, %s)"] * len(ops))
                params: list[object] = []
                for op in ops:
                    p = parse_op_id(op.op_id)
                    params += [op.op_id, p.device, p.seq, op_hash(encode_op(op))]
                conn.execute(
                    "insert into compacted_ops (op_id, device, op_seq, op_hash)"  # noqa: S608 (placeholders only)
                    f" values {values} on conflict do nothing",
                    params,
                )
            conn.execute("delete from feed where record = %s and pos <= %s", (record, last))
            conn.execute(
                "insert into feed (pos, kind, record, op, scopes)"
                " values (%s, 'snapshot', %s, %s::jsonb, %s::text[])",
                (last, record, canonical_json(replica.snapshot_record(record).to_json()), row[0]),
            )
            ops_folded += len(ops)
        # Entries a device can no longer retry (it has pushed past them) are no longer needed.
        pruned = conn.execute(
            "delete from compacted_ops c using devices d"
            " where c.device = d.device_id and c.op_seq < d.push_floor"
        ).rowcount
        return {
            "watermark": watermark,
            "records": len(candidates),
            "opsFolded": ops_folded,
            "tombstonesPruned": max(pruned, 0),
        }
