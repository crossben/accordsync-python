# ADR-Y06: the migration lock is Kysely's advisory lock, not the lock row

**Status:** accepted (corrects the lock described in ADR-Y03)

## Context

ADR-Y03 says migrations run under the `kysely_migration_lock` row, `FOR UPDATE`. Reading Kysely
0.29 (the version `@accordsync/server` ships with), its `PostgresAdapter` does not lock that row: it
takes a **session-level advisory lock**, `pg_advisory_lock(3853314791062309107)`, around one
transaction that runs every pending migration. The `kysely_migration_lock` table and its
`migration_lock` row (`is_locked` 0) are still created, but only for other dialects.

## Decision

`accordsync_server.migrate()` does exactly what Kysely's `Migrator.migrateToLatest()` does on
PostgreSQL:

1. `create table if not exists kysely_migration (name varchar(255) not null primary key,
   timestamp varchar(255) not null)` and `kysely_migration_lock (id varchar(255) not null primary
   key, is_locked integer default 0 not null)`, plus the `migration_lock` row;
2. `pg_advisory_lock(3853314791062309107)` (released in `finally`);
3. one transaction: read the ledger, refuse an unknown or out-of-order executed migration, run the
   pending migrations (the SQL Kysely generates for `0001_meta` … `0006_compacted_op_hash`,
   statement for statement), insert each name with `new Date().toISOString()`'s format.

A TypeScript and a Python server starting at once on the same empty database therefore serialise
on the same lock, and either one's ledger is complete for the other. Checked both ways (pytest
`test_migrations.py` with `ACCORD_APP_DIR`, and a `pg_dump --schema-only` diff: identical).

## Consequences

If Kysely changes its lock (a major upgrade of `@accordsync/server`), this module must follow;
the cross-server ledger tests in CI catch a mismatch in the schema or ledger, not in the lock, so
a Kysely upgrade in the Accord repository needs a look at this ADR.
