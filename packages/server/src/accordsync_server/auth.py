"""Verifies `Authorization: Bearer <jwt>` against the app's JWKS (or a dev secret)."""

from __future__ import annotations

import re
from collections.abc import Callable
from typing import Any

import jwt
from jwt.types import Options

from .define import Auth, Claims


class AuthError(Exception):
    """The request is not authenticated: answered 401."""


Verifier = Callable[[str | None], Claims]

_BEARER = re.compile(r"Bearer (.+)")
ASYMMETRIC = ["RS256", "RS384", "RS512", "PS256", "PS384", "PS512", "ES256", "ES384", "ES512"]
ASYMMETRIC.append("EdDSA")


def create_verifier(auth: Auth) -> Verifier:
    """Like the TypeScript server (jose): `exp` and `nbf` checked when present, `iss` and `aud`
    only when configured, `sub` required. With a JWKS, keys are fetched once and cached."""
    jwks = (
        jwt.PyJWKClient(auth.jwks_url, cache_keys=True, lifespan=auth.jwks_cache_seconds)
        if auth.jwks_url
        else None
    )
    options: Options = {"verify_aud": auth.audience is not None, "verify_iat": False}

    def verify(authorization: str | None) -> Claims:
        m = _BEARER.fullmatch(authorization or "")
        if not m:
            raise AuthError("missing bearer token")
        token = m[1]
        try:
            if jwks is not None:
                key: Any = jwks.get_signing_key_from_jwt(token).key
                algorithms = ASYMMETRIC
            else:
                key = auth.hs256_secret
                algorithms = ["HS256"]
            payload: dict[str, Any] = jwt.decode(
                token,
                key,
                algorithms=algorithms,
                issuer=auth.issuer,
                audience=auth.audience,
                options=options,
            )
        except (jwt.PyJWTError, ValueError, TypeError) as e:
            raise AuthError(f"invalid token: {e}") from e
        sub = payload.get("sub")
        if not isinstance(sub, str) or sub == "":
            raise AuthError('token has no "sub" claim')
        return payload

    return verify
