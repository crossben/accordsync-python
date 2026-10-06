"""Serves the example Django project with waitress (threaded WSGI).

    ACCORD_DATABASE_URL=postgresql://... uv run python examples/django_app/manage.py accord_migrate
    ACCORD_DATABASE_URL=postgresql://... uv run python examples/django_app/serve.py

Sync API on ACCORD_PORT (default 8857). With ACCORD_CONTROL=1, the conformance suite's test-only
control API on ACCORD_CONTROL_PORT (default 8858), in the same process so that its reset clears
the rate limits the views use: never enable it outside tests.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))
os.environ.setdefault(
    "ACCORD_PROFILE",
    str(Path(__file__).resolve().parents[2] / "contract" / "conformance" / "profile.json"),
)
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "settings")

import django
import waitress
from accordsync_django.conf import get_server
from conformance_server import control_wsgi, make_control, serve
from django.core.wsgi import get_wsgi_application


def main() -> None:
    django.setup()
    application = get_wsgi_application()
    if os.environ.get("ACCORD_CONTROL") == "1":
        port = int(os.environ.get("ACCORD_CONTROL_PORT", "8858"))
        serve(control_wsgi(make_control(get_server())), port)
        print(f"control API (tests only) on :{port}", flush=True)
    waitress.serve(
        application,
        host="127.0.0.1",
        port=int(os.environ.get("ACCORD_PORT", "8857")),
        threads=32,
        backlog=2048,
        connection_limit=1000,
        channel_request_lookahead=0,
        _quiet=True,
    )


if __name__ == "__main__":
    main()
