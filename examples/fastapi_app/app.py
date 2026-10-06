"""A FastAPI application serving Accord with the conformance profile.

    ACCORD_DATABASE_URL=postgresql://... uv run python examples/fastapi_app/app.py

Serves the sync API on ACCORD_PORT (default 8855) with uvicorn (endpoints run in its thread pool).
With ACCORD_CONTROL=1 it also serves the conformance suite's test-only control API on
ACCORD_CONTROL_PORT (default 8856): never enable that outside tests. The definition is the
conformance profile (`tools/conformance_server.py`, which reads ACCORD_PROFILE when set).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
os.environ.setdefault(
    "ACCORD_PROFILE",
    str(Path(__file__).resolve().parents[2] / "contract" / "conformance" / "profile.json"),
)

import uvicorn
from accordsync_fastapi import accord_router
from accordsync_server import AccordServer, create_pool, migrate
from conformance_server import control_wsgi, definition, make_control, serve
from fastapi import FastAPI


def main() -> None:
    database_url = os.environ.get("ACCORD_DATABASE_URL")
    if not database_url:
        sys.exit("ACCORD_DATABASE_URL is required")
    migrate(database_url)
    # One process owns the pool: the control API's reset must clear this process's rate limits.
    server = AccordServer(definition, create_pool(database_url))
    server.pool.wait()
    app = FastAPI(title="Accord example")
    app.include_router(accord_router(server))
    if os.environ.get("ACCORD_CONTROL") == "1":
        port = int(os.environ.get("ACCORD_CONTROL_PORT", "8856"))
        serve(control_wsgi(make_control(server)), port)
        print(f"control API (tests only) on :{port}", flush=True)
    uvicorn.run(
        app,
        host="127.0.0.1",
        port=int(os.environ.get("ACCORD_PORT", "8855")),
        log_level="warning",
        backlog=2048,
    )


if __name__ == "__main__":
    main()
