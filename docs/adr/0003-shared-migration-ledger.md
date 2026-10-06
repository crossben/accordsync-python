# ADR-Y03: share the TypeScript server's database and migration ledger

**Status:** accepted

## Decision

The Python server uses the exact PostgreSQL schema of `@accordsync/server` and records migrations
in the same ledger: Kysely's `kysely_migration` table, same migration names (`0001_meta` …
`0005_concurrent_pushes`), same lock (`kysely_migration_lock`, row `migration_lock`, `FOR UPDATE`).
The PHP port made the same decision (its ADR-P02). A database created by any of the three servers is
recognised and upgraded by the others; new migrations are written in the Accord repository first.

## Why

Clients must not care which server they talk to. The mixed-server fleet test runs a TypeScript and a
Python server on one database at the same time, which only works with one schema and one ledger.
