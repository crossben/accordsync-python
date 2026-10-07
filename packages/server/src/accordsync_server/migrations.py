"""The TypeScript server's migrations, as SQL, in its Kysely ledger (ADR-Y03, ADR-Y06).

Migrations are append-only and identical to `app/packages/server/src/migrations/*.ts`: a database
migrated by either server is recognised by the other. Like Kysely's `Migrator`, the ledger tables
are created first, then pending migrations run in one transaction under Kysely's session advisory
lock, each recorded in `kysely_migration` with an ISO timestamp.
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime

import psycopg

MIGRATION_TABLE = "kysely_migration"
LOCK_TABLE = "kysely_migration_lock"
LOCK_ROW = "migration_lock"
KYSELY_LOCK_ID = 3853314791062309107
"""Kysely's `PostgresAdapter` migration lock (a session-level advisory lock)."""


def _m0001(cur: psycopg.Cursor[tuple[object, ...]]) -> None:
    cur.execute('create table "accord_meta" ("key" text primary key, "value" text not null)')
    cur.execute("insert into accord_meta (key, value) values ('schema_created_at', now()::text)")


def _m0002(cur: psycopg.Cursor[tuple[object, ...]]) -> None:
    cur.execute(
        'create table "feed" ("seq" bigserial primary key,'
        " \"kind\" text not null check (kind in ('op', 'scope')),"
        ' "record" text not null, "op_id" text unique, "op" jsonb,'
        ' "scopes" text[] not null, "scopes_before" text[],'
        ' constraint "feed_shape" check ('
        "(kind = 'op' and op_id is not null and op is not null and scopes_before is null)\n"
        "       or (kind = 'scope' and op_id is null and op is null"
        " and scopes_before is not null)))"
    )
    cur.execute('create index "feed_record_seq" on "feed" ("record", "seq")')
    cur.execute('create index "feed_scopes" on "feed" using gin ("scopes")')
    cur.execute(
        """
    create function accord_feed_guard() returns trigger language plpgsql as $$
    begin
      if tg_op = 'DELETE' and current_setting('accord.compaction', true) = 'on' then
        return old;
      end if;
      raise exception 'accord: the feed is append-only (% refused)', tg_op;
    end $$"""
    )
    cur.execute(
        """
    create trigger accord_feed_append_only before update or delete on feed
    for each row execute function accord_feed_guard()"""
    )
    cur.execute('create table "records" ("record" text primary key, "scopes" text[] not null)')
    cur.execute(
        'create table "devices" ("device_id" text primary key, "sub" text not null,'
        ' "read_keys" text[], "first_seen" timestamptz default now() not null,'
        ' "last_seen" timestamptz default now() not null)'
    )


def _m0003(cur: psycopg.Cursor[tuple[object, ...]]) -> None:
    cur.execute("alter table feed drop constraint feed_kind_check")
    cur.execute("alter table feed drop constraint feed_shape")
    cur.execute(
        "alter table feed add constraint feed_kind_check"
        " check (kind in ('op', 'scope', 'snapshot'))"
    )
    cur.execute(
        """alter table feed add constraint feed_shape check (
       (kind = 'op' and op_id is not null and op is not null and scopes_before is null)
    or (kind = 'scope' and op_id is null and op is null and scopes_before is not null)
    or (kind = 'snapshot' and op_id is null and op is not null and scopes_before is null))"""
    )
    cur.execute('create table "compacted_ops" ("op_id" text primary key)')
    cur.execute(
        'alter table "devices" add column "cursor" bigint default 0 not null,'
        ' add column "needs_resync" boolean default false not null'
    )


def _m0004(cur: psycopg.Cursor[tuple[object, ...]]) -> None:
    cur.execute('alter table "records" add column "state" jsonb')


def _m0005(cur: psycopg.Cursor[tuple[object, ...]]) -> None:
    cur.execute("select coalesce(max(seq), 0) + 1 as next from feed")
    row = cur.fetchone()
    assert row is not None
    offset = int(str(row[0]))
    # A constant, so the column default and every query agree on it forever.
    cur.execute(
        "create function accord_xid_offset() returns bigint language sql immutable as"
        f" $$ select {offset}::bigint $$"
    )
    cur.execute(
        """create function accord_pos() returns bigint language sql volatile as $$
      select pg_current_xact_id()::text::bigint + accord_xid_offset() $$"""
    )
    cur.execute(
        """create function accord_horizon() returns bigint language sql volatile as $$
      select pg_snapshot_xmin(pg_current_snapshot())::text::bigint + accord_xid_offset() $$"""
    )
    cur.execute("alter table feed add column pos bigint")
    cur.execute("select set_config('accord.compaction', 'on', true)")
    cur.execute("alter table feed disable trigger accord_feed_append_only")
    cur.execute("update feed set pos = seq")
    cur.execute("alter table feed enable trigger accord_feed_append_only")
    cur.execute("alter table feed alter column pos set not null")
    cur.execute("alter table feed alter column pos set default accord_pos()")
    cur.execute('create index "feed_pos_seq" on "feed" ("pos", "seq")')
    cur.execute("update devices set cursor = 0, needs_resync = true")
    cur.execute("alter table devices add column push_floor bigint not null default 0")
    cur.execute("alter table devices add column max_op_seq bigint not null default 0")
    cur.execute(
        """update devices d set max_op_seq = coalesce((select max(split_part(op_id, ':', 2)::bigint)
      from feed f where f.kind = 'op' and split_part(f.op_id, ':', 1) = d.device_id), 0)"""
    )
    cur.execute("alter table compacted_ops add column device text")
    cur.execute("alter table compacted_ops add column op_seq bigint")
    cur.execute(
        "update compacted_ops set device = split_part(op_id, ':', 1),"
        " op_seq = split_part(op_id, ':', 2)::bigint"
    )
    cur.execute("alter table compacted_ops alter column device set not null")
    cur.execute("alter table compacted_ops alter column op_seq set not null")
    cur.execute('create index "compacted_ops_device" on "compacted_ops" ("device", "op_seq")')


def _m0006(cur: psycopg.Cursor[tuple[object, ...]]) -> None:
    cur.execute('alter table "compacted_ops" add column "op_hash" text')


def _m0007(cur: psycopg.Cursor[tuple[object, ...]]) -> None:
    # A scope delta stays pending until the device shows it received it (ADR-0011, update of
    # 2026-10-07).
    cur.execute("alter table devices add column delta_keys text[], add column delta_cursor bigint")


MIGRATIONS: tuple[tuple[str, Callable[[psycopg.Cursor[tuple[object, ...]]], None]], ...] = (
    ("0001_meta", _m0001),
    ("0002_sync", _m0002),
    ("0003_compaction", _m0003),
    ("0004_record_state", _m0004),
    ("0005_concurrent_pushes", _m0005),
    ("0006_compacted_op_hash", _m0006),
    ("0007_pending_scope_delta", _m0007),
)
NAMES = tuple(name for name, _ in MIGRATIONS)


def _iso_now() -> str:
    """`new Date().toISOString()`: milliseconds and a `Z`."""
    now = datetime.now(UTC)
    return now.strftime("%Y-%m-%dT%H:%M:%S.") + f"{now.microsecond // 1000:03d}Z"


def migrate(
    conninfo_or_conn: str | psycopg.Connection[tuple[object, ...]], target: str | None = None
) -> list[str]:
    """Runs pending migrations up to `target` (a migration name; the latest when omitted).

    Returns the names it ran (empty when already up to date).
    """
    if target is not None and target not in NAMES:
        raise ValueError(f"unknown migration {target}")
    if isinstance(conninfo_or_conn, str):
        with psycopg.connect(conninfo_or_conn, autocommit=True) as conn:
            return _migrate(conn, target)
    return _migrate(conninfo_or_conn, target)


def _migrate(conn: psycopg.Connection[tuple[object, ...]], target: str | None) -> list[str]:
    if not conn.autocommit:
        raise ValueError("migrate needs an autocommit connection (it runs its own transaction)")
    conn.execute(
        f'create table if not exists "{MIGRATION_TABLE}"'
        ' ("name" varchar(255) not null primary key, "timestamp" varchar(255) not null)'
    )
    conn.execute(
        f'create table if not exists "{LOCK_TABLE}"'
        ' ("id" varchar(255) not null primary key, "is_locked" integer default 0 not null)'
    )
    conn.execute(
        f'insert into "{LOCK_TABLE}" ("id", "is_locked") values (%s, 0) on conflict do nothing',  # noqa: S608 (placeholders only)
        (LOCK_ROW,),
    )
    conn.execute(f"select pg_advisory_lock({KYSELY_LOCK_ID})")
    try:
        ran: list[str] = []
        with conn.transaction(), conn.cursor() as cur:
            cur.execute(f'select "name" from "{MIGRATION_TABLE}" order by "timestamp", "name"')  # noqa: S608 (placeholders only)
            done = [str(r[0]) for r in cur.fetchall()]
            for name in done:
                if name not in NAMES:
                    raise RuntimeError(
                        f"corrupted migrations: previously executed migration {name} is missing"
                    )
            if done != list(NAMES[: len(done)]):
                raise RuntimeError(f"corrupted migrations: executed out of order: {done}")
            end = len(NAMES) if target is None else NAMES.index(target) + 1
            for name, up in MIGRATIONS[len(done) : end]:
                up(cur)
                cur.execute(
                    f'insert into "{MIGRATION_TABLE}" ("name", "timestamp") values (%s, %s)',  # noqa: S608 (placeholders only)
                    (name, _iso_now()),
                )
                ran.append(name)
        return ran
    finally:
        conn.execute(f"select pg_advisory_unlock({KYSELY_LOCK_ID})")
