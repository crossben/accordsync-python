"""The Python server configured with the conformance profile, plus the test-only control API.

Mirrors `app/conformance/reference-server.ts` (see `app/conformance/PROFILE.md`). Never expose the
control port outside a test environment.

    ACCORD_DATABASE_URL=postgresql://... ACCORD_PORT=8811 ACCORD_CONTROL_PORT=8812 \\
        uv run python tools/conformance_server.py

Then, from the Accord repository's `app/`:

    ACCORD_URL=http://127.0.0.1:8811 ACCORD_CONTROL_URL=http://127.0.0.1:8812 \\
        pnpm --filter @accordsync/conformance test

ACCORD_PROFILE may name a `profile.json` to read instead of the built-in copy below.
"""

from __future__ import annotations

import json
import logging
import os
import socketserver
import sys
import threading
import time
from collections.abc import Callable, Iterable, Mapping
from typing import Any
from urllib.parse import parse_qs
from wsgiref.simple_server import WSGIRequestHandler, WSGIServer, make_server

import jwt
from accordsync_core import conflict, counter, define_schema, lww, set_
from accordsync_server import (
    Access,
    AccordServer,
    Auth,
    Compaction,
    Limits,
    RateLimit,
    RateLimits,
    ScopedRecord,
    compact,
    create_pool,
    define_server,
    migrate,
)

PROFILE: dict[str, Any] = {
    "auth": {
        "hs256Secret": "accord-conformance-secret-at-least-32-bytes",
        "issuer": "accord-conformance",
    },
    "limits": {
        "maxPushOps": 20,
        "maxPullLimit": 10,
        "maxScopeDelta": 3,
        "maxBodyBytes": 16384,
        "maxSkewMs": 60000,
    },
    "rateLimit": {
        "perDevice": {"perMinute": 6000, "burst": 100},
        "perUser": {"perMinute": 12000, "burst": 200},
    },
    "compaction": {"minOps": 2, "deviceTtlDays": 30, "intervalMs": 0},
}
if os.environ.get("ACCORD_PROFILE"):
    with open(os.environ["ACCORD_PROFILE"], encoding="utf-8") as f:
        PROFILE = json.load(f)


def strings(v: object) -> list[str]:
    return [x for x in v if isinstance(x, str)] if isinstance(v, list) else []


def key(prefix: str, v: object) -> list[str]:
    return [f"{prefix}:{v}"] if isinstance(v, str) else []


def access(claims: Mapping[str, Any]) -> Access:
    write = [f"agent:{claims['sub']}", *(f"zone:{z}" for z in strings(claims.get("zones")))]
    readonly = [f"zone:{z}" for z in strings(claims.get("readonly_zones"))]
    return Access(read=[*write, *readonly], write=write)


def dossier_scopes(r: ScopedRecord) -> list[str]:
    return [*key("agent", r.fields.get("agent")), *key("zone", r.fields.get("zone"))]


limits = PROFILE["limits"]
rate = PROFILE["rateLimit"]
definition = define_server(
    schema=define_schema(
        {
            "dossier": {
                "agent": lww(),
                "zone": lww(),
                "client_name": lww(),
                "status": conflict(),
                "visits": counter(),
                "docs": set_(),
            }
        }
    ),
    scopes={"dossier": dossier_scopes},
    access=access,
    auth=Auth.hs256(PROFILE["auth"]["hs256Secret"], issuer=PROFILE["auth"].get("issuer")),
    limits=Limits(
        max_push_ops=limits["maxPushOps"],
        max_pull_limit=limits["maxPullLimit"],
        max_scope_delta=limits["maxScopeDelta"],
        max_body_bytes=limits["maxBodyBytes"],
        max_skew_ms=limits["maxSkewMs"],
    ),
    rate_limit=RateLimits(
        per_device=RateLimit(rate["perDevice"]["perMinute"], rate["perDevice"].get("burst")),
        per_user=RateLimit(rate["perUser"]["perMinute"], rate["perUser"].get("burst")),
    ),
    compaction=Compaction(
        min_ops=PROFILE["compaction"]["minOps"],
        device_ttl_days=PROFILE["compaction"]["deviceTtlDays"],
        interval_ms=PROFILE["compaction"]["intervalMs"],
    ),
)


class HttpError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def make_control(server: AccordServer) -> Callable[[str, str, str], object]:
    secret = PROFILE["auth"]["hs256Secret"]
    issuer = PROFILE["auth"].get("issuer")

    def control(method: str, path: str, query: str) -> object:
        q = parse_qs(query, keep_blank_values=True)
        if method == "GET" and path == "/token":
            sub = (q.get("sub") or [""])[0]
            if not sub:
                raise HttpError(400, "sub is required")
            now = int(time.time())
            claims: dict[str, Any] = {"sub": sub, "iat": now}
            if issuer:
                claims["iss"] = issuer
            if "zone" in q:
                claims["zones"] = q["zone"]
            if "readonly_zone" in q:
                claims["readonly_zones"] = q["readonly_zone"]
            claims["exp"] = now + int(float((q.get("exp_in") or ["3600"])[0]))
            return {"token": jwt.encode(claims, secret, algorithm="HS256")}
        if method == "POST" and path == "/reset":
            with server.pool.connection() as conn:
                conn.execute("truncate feed, records, devices, compacted_ops restart identity")
            server.reset_rate_limits()
            return {}
        if method == "POST" and path == "/compact":
            return compact(server.pool, server.definition)
        if method == "POST" and path == "/age-device":
            device = (q.get("device") or [""])[0]
            try:
                days = float((q.get("days") or [""])[0])
            except ValueError:
                days = float("nan")
            if not device or days != days or days in (float("inf"), float("-inf")):
                raise HttpError(400, "device and days are required")
            with server.pool.connection() as conn:
                n = conn.execute(
                    "update devices set last_seen = now() - make_interval(days => %s::int)"
                    " where device_id = %s",
                    (int(days), device),
                ).rowcount
            if n == 0:
                raise HttpError(404, "unknown device")
            return {}
        raise HttpError(404, "not found")

    return control


def control_wsgi(
    control: Callable[[str, str, str], object],
) -> Callable[[Mapping[str, Any], Callable[..., object]], Iterable[bytes]]:
    def app(environ: Mapping[str, Any], start_response: Callable[..., object]) -> Iterable[bytes]:
        try:
            status, body = (
                200,
                control(
                    str(environ["REQUEST_METHOD"]),
                    str(environ.get("PATH_INFO", "/")),
                    str(environ.get("QUERY_STRING", "")),
                ),
            )
        except HttpError as e:
            status, body = e.status, {"error": str(e)}
        except Exception as e:
            logging.exception("control API")
            status, body = 500, {"error": str(e)}
        start_response(f"{status} X", [("Content-Type", "application/json")])
        return [json.dumps(body).encode()]

    return app


class ThreadingWSGIServer(socketserver.ThreadingMixIn, WSGIServer):
    daemon_threads = True
    request_queue_size = 1024


class QuietHandler(WSGIRequestHandler):
    def log_message(self, format: str, *args: Any) -> None:
        pass


def serve(app: Any, port: int) -> ThreadingWSGIServer:
    httpd = make_server(
        "127.0.0.1", port, app, server_class=ThreadingWSGIServer, handler_class=QuietHandler
    )
    assert isinstance(httpd, ThreadingWSGIServer)
    threading.Thread(target=httpd.serve_forever, daemon=True).start()
    return httpd


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    database_url = os.environ.get("ACCORD_DATABASE_URL")
    if not database_url:
        sys.exit("ACCORD_DATABASE_URL is required")
    port = int(os.environ.get("ACCORD_PORT", "8811"))
    control_port = int(os.environ.get("ACCORD_CONTROL_PORT", "8812"))
    migrate(database_url)
    pool = create_pool(database_url)
    pool.wait()
    server = AccordServer(definition, pool)
    serve(server.wsgi, port)
    serve(control_wsgi(make_control(server)), control_port)
    print(f"accord python server on :{port}, control API on :{control_port}", flush=True)
    threading.Event().wait()


if __name__ == "__main__":
    main()
