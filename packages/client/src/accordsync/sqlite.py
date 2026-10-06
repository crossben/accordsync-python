"""Durable storage in SQLite, with the tables and SQL of the TypeScript `SqliteStorage`."""

from __future__ import annotations

import json
import os
import sqlite3
import threading
from typing import Any

from accordsync_core import RecordSnapshot

from .storage import StorageSnapshot, StorageTx, StoredMeta

_SCHEMA = (
    "create table if not exists accord_ops (op_id text primary key, body text not null)",
    "create table if not exists accord_outbox (op_id text primary key)",
    "create table if not exists accord_snapshots (record text primary key, body text not null)",
    "create table if not exists accord_meta (k text primary key, v text not null)",
)


def _json(v: Any) -> str:
    # Like JSON.stringify: compact. ASCII escapes keep lone surrogates storable as UTF-8 text.
    return json.dumps(v, separators=(",", ":"), allow_nan=False)


class SqliteStorage:
    """Tables are prefixed `accord_`, so it can share an app's database.

    Takes a file path or an open `sqlite3.Connection` (opened with `check_same_thread=False` if
    several threads use the client). Every commit runs in one `begin immediate` transaction;
    commits from several threads are serialised by a lock.
    """

    def __init__(self, path_or_connection: str | os.PathLike[str] | sqlite3.Connection) -> None:
        if isinstance(path_or_connection, sqlite3.Connection):
            self._db = path_or_connection
            self._owned = False
        else:
            self._db = sqlite3.connect(
                path_or_connection, check_same_thread=False, isolation_level=None
            )
            self._owned = True
        self._lock = threading.RLock()
        self._ready = False

    def _init(self) -> None:
        if not self._ready:
            for sql in _SCHEMA:
                self._db.execute(sql)
            if self._db.in_transaction:
                self._db.commit()
            self._ready = True

    def load(self) -> StorageSnapshot:
        with self._lock:
            self._init()
            db = self._db
            meta = db.execute("select v from accord_meta where k = 'meta'").fetchall()
            snapshots = db.execute("select body from accord_snapshots").fetchall()
            ops = db.execute("select body from accord_ops").fetchall()
            outbox = db.execute("select op_id from accord_outbox").fetchall()
            return StorageSnapshot(
                meta=StoredMeta.from_json(json.loads(meta[0][0])) if meta else None,
                snapshots=[RecordSnapshot.from_json(json.loads(r[0])) for r in snapshots],
                ops=[json.loads(r[0]) for r in ops],
                outbox=[r[0] for r in outbox],
            )

    def commit(self, tx: StorageTx) -> None:
        with self._lock:
            self._init()
            db = self._db
            db.execute("begin immediate")
            try:
                if tx.clear_ops:
                    db.execute("delete from accord_ops")
                    db.execute("delete from accord_snapshots")
                for r in tx.delete_snapshots:
                    db.execute("delete from accord_snapshots where record = ?", (r,))
                for snap in tx.put_snapshots:
                    db.execute(
                        "insert or replace into accord_snapshots (record, body) values (?, ?)",
                        (snap.record, _json(snap.to_json())),
                    )
                for i in tx.delete_ops:
                    db.execute("delete from accord_ops where op_id = ?", (i,))
                for op in tx.put_ops:
                    db.execute(
                        "insert or replace into accord_ops (op_id, body) values (?, ?)",
                        (op["op_id"], _json(op)),
                    )
                for i in tx.outbox_add:
                    db.execute("insert or ignore into accord_outbox (op_id) values (?)", (i,))
                for i in tx.outbox_delete:
                    db.execute("delete from accord_outbox where op_id = ?", (i,))
                if tx.meta is not None:
                    db.execute(
                        "insert or replace into accord_meta (k, v) values ('meta', ?)",
                        (_json(tx.meta.to_json()),),
                    )
                db.execute("commit")
            except BaseException:
                db.execute("rollback")
                raise

    def close(self) -> None:
        with self._lock:
            if self._owned:
                self._db.close()
