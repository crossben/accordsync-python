"""`python -m accordsync_server migrate|compact`.

`migrate` needs only ACCORD_DATABASE_URL. `compact` also needs the server definition:
`--definition module:attribute` (or ACCORD_SERVER) naming a `ServerDefinition`.
"""

from __future__ import annotations

import argparse
import importlib
import json
import os
import sys

from .compact import compact
from .db import create_pool
from .define import ServerDefinition
from .migrations import migrate


def load_definition(spec: str) -> ServerDefinition:
    module, _, attr = spec.partition(":")
    value = getattr(importlib.import_module(module), attr or "server")
    if not isinstance(value, ServerDefinition):
        raise SystemExit(f"{spec} is not a ServerDefinition (use define_server(...))")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m accordsync_server")
    parser.add_argument("command", choices=["migrate", "compact"])
    parser.add_argument("--database-url", default=os.environ.get("ACCORD_DATABASE_URL"))
    parser.add_argument("--definition", default=os.environ.get("ACCORD_SERVER"))
    args = parser.parse_args(argv)
    if not args.database_url:
        parser.error("ACCORD_DATABASE_URL (or --database-url) is required")
    if args.command == "migrate":
        ran = migrate(args.database_url)
        print(f"applied {len(ran)} migration(s): {', '.join(ran)}" if ran else "up to date")
        return 0
    if not args.definition:
        parser.error("compact needs --definition module:attribute (or ACCORD_SERVER)")
    definition = load_definition(args.definition)
    with create_pool(args.database_url, size=2) as pool:
        print(json.dumps(compact(pool, definition)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
