"""Wiring tests: settings, the system check, commands and views passing requests through."""

from __future__ import annotations

import os
from collections.abc import Mapping
from typing import Any

import pytest
from accordsync_django import PROTOCOL_VERSION, conf, views
from accordsync_django.checks import check_accord
from accordsync_server import AccordServer, Response
from django.core.exceptions import ImproperlyConfigured
from django.core.management import call_command, get_commands
from django.test import Client, override_settings


class DeadPool:
    def connection(self, **_: Any) -> Any:
        raise RuntimeError("no database")


class StubServer(AccordServer):
    def __init__(self) -> None:
        super().__init__(conf.load_definition(), DeadPool())  # type: ignore[arg-type]
        self.calls: list[tuple[str, str, Any, dict[str, str], bytes]] = []

    def handle(
        self, method: str, path: str, query: Any, headers: Mapping[str, str], body: bytes = b""
    ) -> Response:
        self.calls.append((method, path, query, dict(headers), body))
        return Response(418, [("Content-Type", "text/plain"), ("Vary", "A"), ("Vary", "B")], b"s")


@pytest.fixture
def stub(monkeypatch: pytest.MonkeyPatch) -> StubServer:
    server = StubServer()
    monkeypatch.setattr(views, "get_server", lambda: server)
    return server


def test_speaks_protocol_version_1() -> None:
    assert PROTOCOL_VERSION == 1


def test_passes_raw_request_and_response_through(stub: StubServer) -> None:
    res = Client().post(
        "/v1/push?a=1&a=2",
        data=b'{"ops": []}',
        content_type="application/json",
        headers={"Accord-Device": "d1"},
    )
    assert res.status_code == 418
    assert res.content == b"s"
    assert res["Vary"] == "A, B"
    method, path, query, headers, body = stub.calls[0]
    assert (method, path, query, body) == ("POST", "/v1/push", "a=1&a=2", b'{"ops": []}')
    assert headers["Accord-Device"] == "d1"


@pytest.mark.parametrize(("url", "path"), [("/v1/pull", "/v1/pull"), ("/health", "/health")])
@pytest.mark.parametrize("method", ["get", "head", "options", "delete"])
def test_every_method_reaches_handle(stub: StubServer, url: str, path: str, method: str) -> None:
    assert getattr(Client(), method)(url).status_code == 418
    assert stub.calls[0][:2] == (method.upper(), path)


def test_body_is_cut_one_byte_past_the_limit(stub: StubServer) -> None:
    Client().post("/v1/push", data=b"x" * 10_000, content_type="application/json")
    assert stub.calls[0][4] == b"x" * 101


def test_views_are_csrf_exempt_and_not_atomic(stub: StubServer) -> None:
    for view in (views.push, views.pull, views.health):
        assert getattr(view, "csrf_exempt", False)
        assert getattr(view, "_non_atomic_requests", None)
    assert Client(enforce_csrf_checks=True).post("/v1/push").status_code == 418


def test_check_passes() -> None:
    with override_settings(ACCORD_DATABASE_URL="postgresql://h/db"):
        assert check_accord() == []


@pytest.mark.parametrize(
    ("spec", "message"),
    [
        (None, "must be set"),
        ("nope_module:x", "cannot be imported"),
        ("accordsync_django_testdef:not_a_definition", "not a ServerDefinition"),
    ],
)
def test_check_reports_bad_definition(spec: str | None, message: str) -> None:
    with override_settings(ACCORD_SERVER=spec, ACCORD_DATABASE_URL="postgresql://h/db"):
        errors = check_accord()
    assert [e.id for e in errors] == ["accordsync.E001"]
    assert message in errors[0].msg


def test_database_url_from_django_settings(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ACCORD_DATABASE_URL", raising=False)
    db = {
        "ENGINE": "django.db.backends.postgresql",
        "NAME": "app",
        "USER": "me",
        "PASSWORD": "p@ss",
        "HOST": "db",
        "PORT": "5433",
    }
    with override_settings(DATABASES={"default": db}):
        assert conf.database_url() == "postgresql://me:p%40ss@db:5433/app"
    with override_settings(DATABASES={"default": {"ENGINE": "django.db.backends.sqlite3"}}):
        with pytest.raises(ImproperlyConfigured):
            conf.database_url()
        assert [e.id for e in check_accord()] == ["accordsync.E002"]


def test_commands_registered() -> None:
    commands = get_commands()
    assert commands["accord_migrate"] == "accordsync_django"
    assert commands["accord_compact"] == "accordsync_django"


def test_migrate_command_against_postgres() -> None:
    url = os.environ.get("ACCORD_TEST_DATABASE_URL")
    if not url:
        pytest.skip("ACCORD_TEST_DATABASE_URL is not set")
    with override_settings(ACCORD_DATABASE_URL=url):
        call_command("accord_migrate")
        call_command("accord_compact")
