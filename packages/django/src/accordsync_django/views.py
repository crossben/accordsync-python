"""The three endpoints: each passes the raw request to `AccordServer.handle()` (ADR-Y07)."""

from __future__ import annotations

from collections.abc import Callable

from django.db import transaction
from django.http import HttpRequest, HttpResponse
from django.views.decorators.csrf import csrf_exempt

from .conf import get_server


def _view(path: str) -> Callable[[HttpRequest], HttpResponse]:
    @csrf_exempt
    @transaction.non_atomic_requests
    def view(request: HttpRequest) -> HttpResponse:
        server = get_server()
        length = request.META.get("CONTENT_LENGTH") or "0"
        try:
            n = int(length)
        except ValueError:
            n = 0
        # Read the stream directly (not request.body: DATA_UPLOAD_MAX_MEMORY_SIZE must not apply),
        # at most one byte past the limit: a larger body gets 413 without being buffered.
        body = request.read(min(n, server.max_body_bytes + 1)) if n > 0 else b""
        res = server.handle(
            request.method or "GET",
            path,
            request.META.get("QUERY_STRING", ""),
            dict(request.headers.items()),
            body,
        )
        out = HttpResponse(res.body, status=res.status)
        merged: dict[str, list[str]] = {}
        for name, value in res.headers:
            merged.setdefault(name, []).append(value)
        for name, values in merged.items():
            out[name] = ", ".join(values)
        return out

    view.__name__ = "accord_" + path.strip("/").replace("/", "_")
    return view


push = _view("/v1/push")
pull = _view("/v1/pull")
health = _view("/health")
