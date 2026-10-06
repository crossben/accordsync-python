"""Fixtures for the server tests.

Tests that need PostgreSQL use `database_url`: set ACCORD_TEST_DATABASE_URL (a postgresql:// URL)
to a server you may create databases on (each test gets a fresh database, dropped afterwards), e.g.

    docker run -d --name accord-test-pg -e POSTGRES_PASSWORD=accord -p 55432:5432 postgres:16-alpine
    ACCORD_TEST_DATABASE_URL=postgresql://postgres:accord@127.0.0.1:55432/postgres uv run pytest

Without it they are skipped. Tests that run the TypeScript server's code also need ACCORD_APP_DIR
(an Accord checkout's `app/`, with `pnpm install` done) and `node` on the PATH.
"""

from __future__ import annotations

import os
import secrets
import shutil
import subprocess
from collections.abc import Iterator
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

import psycopg
import pytest

TS_TOOL = Path(__file__).parent / "ts" / "ts_tool.mts"


@pytest.fixture
def database_url() -> Iterator[str]:
    admin = os.environ.get("ACCORD_TEST_DATABASE_URL")
    if not admin:
        pytest.skip("ACCORD_TEST_DATABASE_URL is not set (PostgreSQL tests skipped)")
    name = f"accord_test_{secrets.token_hex(6)}"
    with psycopg.connect(admin, autocommit=True) as conn:
        conn.execute(f'create database "{name}"')
    # A URL (not a key=value string): the TypeScript tools take URLs only.
    url = urlsplit(admin)
    try:
        yield urlunsplit(url._replace(path=f"/{name}"))
    finally:
        with psycopg.connect(admin, autocommit=True) as conn:
            conn.execute(f'drop database "{name}" with (force)')


def run_ts(*args: str, stdin: str | None = None) -> str:
    """Runs tests/ts/ts_tool.mts against the TypeScript server's sources, or skips."""
    app = os.environ.get("ACCORD_APP_DIR")
    if not app or not shutil.which("node"):
        pytest.skip("ACCORD_APP_DIR and node are needed to run the TypeScript server's code")
    app_dir = Path(app).resolve()
    node = shutil.which("node")
    assert node is not None
    out = subprocess.run(  # noqa: S603
        [
            node,
            "--import",
            "tsx",
            "--conditions=@accordsync/source",
            str(TS_TOOL),
            *args,
        ],
        cwd=app_dir / "conformance",
        env={**os.environ, "ACCORD_APP_DIR": str(app_dir)},
        input=stdin,
        capture_output=True,
        text=True,
        check=True,
        timeout=120,
    )
    return out.stdout
