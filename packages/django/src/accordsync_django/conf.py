"""Settings: `ACCORD_SERVER` (the definition) and the database the sync server uses.

- `ACCORD_SERVER = "module:attribute"` names a `ServerDefinition` (required).
- `ACCORD_DATABASE_URL` (setting, else environment variable) is a PostgreSQL URL; without it the
  URL is built from `DATABASES["default"]`, which must then use the PostgreSQL backend.
- `ACCORD_POOL_SIZE` (default 20) and `ACCORD_SCHEDULE_COMPACTION` (default True).

The sync server uses its own psycopg pool, never Django's connections (ADR-Y10).
"""

from __future__ import annotations

import atexit
import importlib
import logging
import os
import threading
from typing import Any
from urllib.parse import quote

from accordsync_server import AccordServer, ServerDefinition, compact, create_pool
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured

log = logging.getLogger("accordsync_django")


def load_definition() -> ServerDefinition:
    spec = getattr(settings, "ACCORD_SERVER", None)
    if not spec or not isinstance(spec, str):
        raise ImproperlyConfigured('ACCORD_SERVER must be set to "module:attribute"')
    module, _, attr = spec.partition(":")
    try:
        value = getattr(importlib.import_module(module), attr or "server")
    except (ImportError, AttributeError) as e:
        raise ImproperlyConfigured(f"ACCORD_SERVER {spec!r} cannot be imported: {e}") from e
    if not isinstance(value, ServerDefinition):
        raise ImproperlyConfigured(
            f"ACCORD_SERVER {spec!r} is not a ServerDefinition (use define_server(...))"
        )
    return value


def database_url() -> str:
    url = getattr(settings, "ACCORD_DATABASE_URL", None) or os.environ.get("ACCORD_DATABASE_URL")
    if url:
        return str(url)
    db: dict[str, Any] = settings.DATABASES.get("default", {})
    if db.get("ENGINE") != "django.db.backends.postgresql":
        raise ImproperlyConfigured(
            "set ACCORD_DATABASE_URL, or use django.db.backends.postgresql for the default database"
        )
    user = quote(str(db.get("USER") or ""), safe="")
    password = quote(str(db.get("PASSWORD") or ""), safe="")
    auth = f"{user}:{password}@" if password else (f"{user}@" if user else "")
    host = str(db.get("HOST") or "localhost")
    port = f":{db['PORT']}" if db.get("PORT") else ""
    return f"postgresql://{auth}{host}{port}/{quote(str(db.get('NAME') or ''), safe='')}"


class _Scheduler:
    def __init__(self, server: AccordServer) -> None:
        self.server = server
        self.interval_s = server.definition.compaction.interval_ms / 1000
        self.stop = threading.Event()

    def run(self) -> None:
        while not self.stop.wait(self.interval_s):
            try:
                r = compact(self.server.pool, self.server.definition)
                log.info(
                    "compaction: %d records, %d ops folded below position %d, %d tombstones pruned",
                    r["records"],
                    r["opsFolded"],
                    r["watermark"],
                    r["tombstonesPruned"],
                )
            except Exception:
                log.exception("compaction failed")


_lock = threading.Lock()
_server: AccordServer | None = None


def get_server() -> AccordServer:
    """The process's `AccordServer`, created (pool opened, compaction scheduled) on first use."""
    global _server
    if _server is not None:
        return _server
    with _lock:
        if _server is None:
            server = AccordServer(
                load_definition(),
                create_pool(database_url(), size=int(getattr(settings, "ACCORD_POOL_SIZE", 20))),
            )
            atexit.register(server.pool.close)
            if getattr(settings, "ACCORD_SCHEDULE_COMPACTION", True):
                sched = _Scheduler(server)
                if sched.interval_s > 0:
                    threading.Thread(
                        target=sched.run, name="accord-compaction", daemon=True
                    ).start()
                    atexit.register(sched.stop.set)
            _server = server
    return _server
