"""The shared migration ledger (ADR-Y03, ADR-Y06). Needs ACCORD_TEST_DATABASE_URL."""

from __future__ import annotations

import re

import psycopg
import pytest
from accordsync_server import migrate
from accordsync_server.migrations import NAMES

from .conftest import run_ts


def ledger(url: str) -> list[tuple[str, str]]:
    with psycopg.connect(url) as conn:
        return [
            (str(r[0]), str(r[1]))
            for r in conn.execute('select name, "timestamp" from kysely_migration order by name')
        ]


def columns(url: str) -> list[tuple[object, ...]]:
    with psycopg.connect(url) as conn:
        return conn.execute(
            "select table_name, column_name, data_type, is_nullable, column_default"
            " from information_schema.columns where table_schema = 'public'"
            " order by table_name, column_name"
        ).fetchall()


def test_migrates_once_and_records_kysely_ledger(database_url: str) -> None:
    assert migrate(database_url) == list(NAMES)
    assert migrate(database_url) == []
    rows = ledger(database_url)
    assert [n for n, _ in rows] == list(NAMES)
    for _, ts in rows:
        assert re.fullmatch(r"\d{4}-\d\d-\d\dT\d\d:\d\d:\d\d\.\d{3}Z", ts)
    with psycopg.connect(database_url) as conn:
        assert conn.execute("select id, is_locked from kysely_migration_lock").fetchall() == [
            ("migration_lock", 0)
        ]
        # The feed is append-only, and accord_pos()/accord_horizon() exist.
        conn.execute(
            "insert into feed (kind, record, scopes, scopes_before) values"
            " ('scope', 'dossier:1', '{}', '{}')"
        )
        pos, horizon = conn.execute("select pos, accord_horizon() from feed").fetchone()  # type: ignore[misc]
        assert pos >= horizon
        with pytest.raises(psycopg.errors.RaiseException, match="append-only"):
            conn.execute("delete from feed")


def test_migrates_step_by_step_to_the_same_schema(database_url: str) -> None:
    assert migrate(database_url, target="0003_compaction") == list(NAMES[:3])
    assert migrate(database_url) == list(NAMES[3:])
    assert [n for n, _ in ledger(database_url)] == list(NAMES)


def test_refuses_a_ledger_with_an_unknown_migration(database_url: str) -> None:
    migrate(database_url)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute(
            "insert into kysely_migration values ('9999_future', '2099-01-01T00:00:00.000Z')"
        )
    with pytest.raises(RuntimeError, match="9999_future"):
        migrate(database_url)


def test_a_database_migrated_by_typescript_is_up_to_date_for_python(database_url: str) -> None:
    run_ts("migrate", database_url)
    before = ledger(database_url)
    assert migrate(database_url) == []
    assert ledger(database_url) == before


def test_a_database_migrated_by_python_is_up_to_date_for_typescript(
    database_url: str, tmp_path: object
) -> None:
    migrate(database_url)
    before = ledger(database_url)
    run_ts("migrate", database_url)
    assert ledger(database_url) == before


def test_python_and_typescript_create_the_same_schema(database_url: str) -> None:
    migrate(database_url)
    ours = columns(database_url)
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("drop schema public cascade")
        conn.execute("create schema public")
    run_ts("migrate", database_url)
    assert columns(database_url) == ours


def test_typescript_runs_0007_on_a_database_python_migrated_to_0006(database_url: str) -> None:
    migrate(database_url, target="0006_compacted_op_hash")
    run_ts("migrate", database_url)
    assert [n for n, _ in ledger(database_url)] == list(NAMES)
    assert NAMES[-1] == "0007_pending_scope_delta"
    assert migrate(database_url) == []


def test_python_runs_0007_on_a_database_typescript_left_at_0006(database_url: str) -> None:
    run_ts("migrate", database_url)
    ts_columns = columns(database_url)
    # Kysely's down of 0007, as the TypeScript migration writes it.
    with psycopg.connect(database_url, autocommit=True) as conn:
        conn.execute("alter table devices drop column delta_keys, drop column delta_cursor")
        conn.execute("delete from kysely_migration where name = '0007_pending_scope_delta'")
    assert migrate(database_url) == ["0007_pending_scope_delta"]
    assert columns(database_url) == ts_columns


def test_0007_adds_the_pending_delta_columns(database_url: str) -> None:
    migrate(database_url)
    devices = [c for c in columns(database_url) if c[0] == "devices"]
    assert ("devices", "delta_cursor", "bigint", "YES", None) in devices
    assert ("devices", "delta_keys", "ARRAY", "YES", None) in devices
