"""The framework-agnostic request layer (`app.ts`): one `handle()` call per HTTP request.

Framework adapters (FastAPI, Django, WSGI) only translate their request into
`handle(method, path, query, headers, body)` and the returned `Response` back.
"""

from __future__ import annotations

import json
import logging
import math
import time
from collections.abc import Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any
from urllib.parse import parse_qs

from accordsync_core import PROTOCOL_VERSION, AccordError, assert_node, canonical_json

from .auth import AuthError, Verifier, create_verifier
from .define import ServerDefinition
from .jsnum import js_safe_integer
from .ratelimit import Limiter, RateLimiter
from .sync import (
    BadRequestError,
    Caller,
    ForbiddenError,
    Pool,
    SyncContext,
    device_ttl_ms,
    pull,
    push,
    touch_device,
)

log = logging.getLogger("accordsync_server")

Query = Mapping[str, Sequence[str]] | str


@dataclass(slots=True)
class Response:
    status: int
    headers: list[tuple[str, str]] = field(default_factory=list)
    body: bytes = b""


class TooManyRequestsError(Exception):
    def __init__(self, retry_after_ms: int) -> None:
        super().__init__("too many requests")
        self.retry_after_ms = retry_after_ms


def now_ms() -> int:
    return time.time_ns() // 1_000_000


def _json(status: int, body: object, headers: Iterable[tuple[str, str]] = ()) -> Response:
    return Response(
        status,
        [("Content-Type", "application/json"), *headers],
        canonical_json(body).encode("utf-8"),
    )


def _error(status: int, message: str) -> Response:
    return _json(status, {"error": message})


def _reject_constant(name: str) -> object:
    raise ValueError(f"{name} is not JSON")


class AccordServer:
    """Push, pull and health over one connection pool, with auth, limits, CORS and rate limits."""

    def __init__(
        self,
        definition: ServerDefinition,
        pool: Pool,
        *,
        now: Callable[[], int] = now_ms,
        verifier: Verifier | None = None,
    ) -> None:
        self.definition = definition
        self.pool = pool
        self.ctx = SyncContext(pool, definition, now)
        self._verify = verifier or create_verifier(definition.auth)
        self._limiters: tuple[Limiter, Limiter] | None = None
        self.reset_rate_limits()

    def reset_rate_limits(self) -> None:
        """Starts every rate-limit bucket full again (the conformance control API's reset)."""
        rl = self.definition.rate_limit
        self._limiters = (
            None
            if rl is None
            else (
                RateLimiter(rl.per_device, self.ctx.now),
                RateLimiter(rl.per_user, self.ctx.now),
            )
        )

    @property
    def max_body_bytes(self) -> int:
        """Adapters may stop reading a body past this (it gets 413 anyway)."""
        return self.definition.limits.max_body_bytes

    # ------------------------------------------------------------------ dispatch

    def handle(
        self,
        method: str,
        path: str,
        query: Query,
        headers: Mapping[str, str],
        body: bytes = b"",
    ) -> Response:
        h = {k.lower(): v for k, v in headers.items()}
        q = parse_qs(query, keep_blank_values=True) if isinstance(query, str) else query
        method = method.upper()
        cors = self._cors(method, h)
        if cors is not None and cors[0]:
            res = Response(204, cors[1])
        else:
            try:
                res = self._route(method, path, q, h, body)
            except AuthError as e:
                res = _error(401, str(e))
            except ForbiddenError as e:
                res = _error(403, str(e))
            except BadRequestError as e:
                res = _error(400, str(e))
            except TooManyRequestsError as e:
                res = _error(429, "too many requests")
                res.headers.append(("Retry-After", str(math.ceil(e.retry_after_ms / 1000))))
            except Exception:
                log.exception("accord: internal error")
                res = _error(500, "internal error")
            if cors is not None:
                res.headers += cors[1]
        res.headers.append(("Accord-Protocol", str(PROTOCOL_VERSION)))
        return res

    def _cors(self, method: str, h: Mapping[str, str]) -> tuple[bool, list[tuple[str, str]]] | None:
        """(is a preflight, headers to add), or None when CORS is off for this request."""
        origins = self.definition.cors
        if not origins:
            return None
        origin = h.get("origin")
        allowed = origin if origin in origins else None
        out: list[tuple[str, str]] = []
        if allowed:
            out.append(("Access-Control-Allow-Origin", allowed))
        out.append(("Vary", "Origin"))
        if method == "OPTIONS":
            out += [
                ("Access-Control-Max-Age", "600"),
                ("Access-Control-Allow-Methods", "GET,HEAD,PUT,POST,DELETE,PATCH"),
                ("Access-Control-Allow-Headers", "Authorization,Accord-Device,Content-Type"),
            ]
            return True, out
        out.append(("Access-Control-Expose-Headers", "Accord-Protocol"))
        return False, out

    def _route(
        self,
        method: str,
        path: str,
        q: Mapping[str, Sequence[str]],
        h: Mapping[str, str],
        body: bytes,
    ) -> Response:
        if path == "/health" and method in ("GET", "HEAD"):
            return self.health()
        if path.startswith("/v1/"):
            limit = self.definition.limits.max_body_bytes
            length = js_safe_integer(h.get("content-length", "") or "0")
            if len(body) > limit or (length is not None and length > limit):
                return _error(413, "request body too large")
            if path == "/v1/push" and method == "POST":
                return self._push(h, body)
            if path == "/v1/pull" and method in ("GET", "HEAD"):
                return self._pull(h, q)
        return _error(404, "not found")

    # ------------------------------------------------------------------ routes

    def health(self) -> Response:
        try:
            with self.pool.connection(timeout=5) as conn:
                conn.execute("select 1")
        except Exception:
            return _json(503, {"status": "unavailable", "reason": "database unreachable"})
        return _json(200, {"status": "ok", "protocolVersion": PROTOCOL_VERSION})

    def _caller(self, h: Mapping[str, str]) -> Caller:
        claims = self._verify(h.get("authorization"))
        device_id = h.get("accord-device", "")
        try:
            assert_node(device_id)
        except AccordError:
            raise BadRequestError(
                "Accord-Device header must be a device id ([A-Za-z0-9_-]{1,64})"
            ) from None
        if self._limiters is not None:
            device, user = self._limiters
            wait = max(device.take(device_id), user.take(claims["sub"]))
            if wait > 0:
                raise TooManyRequestsError(wait)
        access = self.definition.access(claims)
        who = Caller(claims["sub"], device_id, tuple(access.read), tuple(access.write))
        touch_device(self.pool, who, device_ttl_ms(self.definition))
        return who

    def _push(self, h: Mapping[str, str], body: bytes) -> Response:
        who = self._caller(h)
        try:
            data: Any = json.loads(body.decode("utf-8", "replace"), parse_constant=_reject_constant)
        except (ValueError, RecursionError):
            raise BadRequestError("body must be JSON") from None
        if not (isinstance(data, dict) and set(data) == {"ops"} and isinstance(data["ops"], list)):
            raise BadRequestError('body must be { "ops": [...] }')
        return _json(200, push(self.ctx, who, data["ops"]))

    def _pull(self, h: Mapping[str, str], q: Mapping[str, Sequence[str]]) -> Response:
        who = self._caller(h)
        cursor = js_safe_integer(_first(q, "cursor", "0"))
        limit = js_safe_integer(_first(q, "limit", "500"))
        if cursor is None or cursor < 0:
            raise BadRequestError("cursor must be an integer ≥ 0")
        if limit is None or limit < 1:
            raise BadRequestError("limit must be an integer ≥ 1")
        return _json(200, pull(self.ctx, who, cursor, limit))

    # ------------------------------------------------------------------ WSGI

    def wsgi(
        self, environ: Mapping[str, Any], start_response: Callable[..., object]
    ) -> list[bytes]:
        """A WSGI application: `AccordServer(...).wsgi` can be served by any WSGI server."""
        headers = {
            k[5:].replace("_", "-"): str(v) for k, v in environ.items() if k.startswith("HTTP_")
        }
        if environ.get("CONTENT_TYPE"):
            headers["content-type"] = str(environ["CONTENT_TYPE"])
        if environ.get("CONTENT_LENGTH"):
            headers["content-length"] = str(environ["CONTENT_LENGTH"])
        length = js_safe_integer(headers.get("content-length", "0")) or 0
        stream = environ["wsgi.input"]
        # Read at most one byte past the limit: a larger body gets 413 without being buffered.
        body = stream.read(min(max(length, 0), self.max_body_bytes + 1)) if length > 0 else b""
        res = self.handle(
            str(environ.get("REQUEST_METHOD", "GET")),
            str(environ.get("PATH_INFO", "/")) or "/",
            str(environ.get("QUERY_STRING", "")),
            headers,
            body,
        )
        reason = _REASONS.get(res.status, "Unknown")
        start_response(f"{res.status} {reason}", res.headers)
        return [res.body]


def _first(q: Mapping[str, Sequence[str]], name: str, default: str) -> str:
    v = q.get(name)
    return v[0] if v else default


_REASONS = {
    200: "OK",
    204: "No Content",
    400: "Bad Request",
    401: "Unauthorized",
    403: "ForbiddenError",
    404: "Not Found",
    413: "Payload Too Large",
    429: "Too Many Requests",
    500: "Internal Server Error",
    503: "Service Unavailable",
}
