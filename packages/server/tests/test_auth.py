from __future__ import annotations

import json
import threading
import time
from collections.abc import Iterator
from http.server import BaseHTTPRequestHandler, HTTPServer
from typing import Any

import jwt
import pytest
from accordsync_server import Auth, AuthError, create_verifier
from cryptography.hazmat.primitives.asymmetric import rsa
from jwt.algorithms import RSAAlgorithm

SECRET = "accord-test-secret-at-least-32-bytes-long"  # noqa: S105 (a test key)


def hs(claims: dict[str, Any], secret: str = SECRET) -> str:
    return "Bearer " + jwt.encode(claims, secret, algorithm="HS256")


def exp(seconds: int = 3600) -> int:
    return int(time.time()) + seconds


def test_hs256_accepts_a_good_token_and_returns_its_claims() -> None:
    verify = create_verifier(Auth.hs256(SECRET, issuer="iss"))
    claims = verify(hs({"sub": "alice", "iss": "iss", "exp": exp(), "zones": ["z"]}))
    assert claims["sub"] == "alice"
    assert claims["zones"] == ["z"]


@pytest.mark.parametrize(
    "header",
    [
        None,
        "",
        "Bearer ",
        "Basic abc",
        "Bearer not-a-jwt",
        hs({"sub": "a", "iss": "iss", "exp": exp()}, "another-secret-at-least-32-bytes-long"),
        hs({"sub": "a", "iss": "iss", "exp": exp(-120)}),
        hs({"sub": "a", "iss": "other", "exp": exp()}),
        hs({"sub": "a", "exp": exp()}),  # no issuer while one is configured
        hs({"iss": "iss", "exp": exp()}),
        hs({"sub": "", "iss": "iss", "exp": exp()}),
        hs({"sub": "a", "iss": "iss", "nbf": exp(600)}),
    ],
)
def test_hs256_refuses(header: str | None) -> None:
    verify = create_verifier(Auth.hs256(SECRET, issuer="iss"))
    with pytest.raises(AuthError):
        verify(header)


def test_audience_is_checked_only_when_configured() -> None:
    token = hs({"sub": "a", "aud": "app", "exp": exp()})
    assert create_verifier(Auth.hs256(SECRET))(token)["sub"] == "a"  # like jose: ignored
    assert create_verifier(Auth.hs256(SECRET, audience="app"))(token)["sub"] == "a"
    with pytest.raises(AuthError):
        create_verifier(Auth.hs256(SECRET, audience="other"))(token)


def test_a_future_iat_is_accepted_like_jose() -> None:
    assert create_verifier(Auth.hs256(SECRET))(hs({"sub": "a", "iat": exp(30)}))["sub"] == "a"


def test_hs256_refuses_an_unsigned_or_asymmetric_token() -> None:
    verify = create_verifier(Auth.hs256(SECRET))
    with pytest.raises(AuthError):
        verify("Bearer " + jwt.encode({"sub": "a"}, "", algorithm="none"))


class _Jwks:
    def __init__(self) -> None:
        self.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        jwk = json.loads(RSAAlgorithm.to_jwk(self.key.public_key()))
        jwk.update(kid="k1", alg="RS256", use="sig")
        self.body = json.dumps({"keys": [jwk]}).encode()
        self.hits = 0

    def token(self, claims: dict[str, Any], kid: str = "k1") -> str:
        return "Bearer " + jwt.encode(claims, self.key, algorithm="RS256", headers={"kid": kid})


@pytest.fixture
def jwks() -> Iterator[tuple[_Jwks, str]]:
    keys = _Jwks()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            keys.hits += 1
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(keys.body)

        def log_message(self, *args: object) -> None:
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield keys, f"http://127.0.0.1:{server.server_address[1]}/jwks.json"
    finally:
        server.shutdown()


def test_jwks_verifies_with_a_cached_key_set(jwks: tuple[_Jwks, str]) -> None:
    keys, url = jwks
    verify = create_verifier(Auth.jwks(url, issuer="https://auth", audience="accord"))
    good = {"sub": "alice", "iss": "https://auth", "aud": "accord", "exp": exp()}
    assert verify(keys.token(good))["sub"] == "alice"
    assert verify(keys.token(good))["sub"] == "alice"
    assert keys.hits == 1  # fetched once, then cached
    for bad in (
        {**good, "aud": "other"},
        {**good, "iss": "https://evil"},
        {**good, "exp": exp(-60)},
    ):
        with pytest.raises(AuthError):
            verify(keys.token(bad))
    with pytest.raises(AuthError):
        verify(keys.token(good, kid="unknown"))
    # An HS256 token signed with anything is refused by a JWKS verifier.
    with pytest.raises(AuthError):
        verify(hs(good))
