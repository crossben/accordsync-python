"""The server definition: schema, scopes, access, auth and limits (`defineServer` in TypeScript)."""

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from typing import Any

from accordsync_core import Schema

Claims = dict[str, Any]
"""The JWT claims Accord verified, as the app's auth server issued them (always with `sub`)."""


@dataclass(frozen=True, slots=True)
class ScopedRecord:
    """A record as scope functions see it: its id and its current field values."""

    id: str
    fields: Mapping[str, object]


@dataclass(frozen=True, slots=True)
class Access:
    """The scope keys a user may read and write."""

    read: Sequence[str]
    write: Sequence[str]


ScopeFn = Callable[[ScopedRecord], Sequence[str]]
AccessFn = Callable[[Claims], Access]


@dataclass(frozen=True, slots=True)
class Auth:
    """How bearer tokens are verified: a JWKS URL (production) or an HS256 secret (development)."""

    jwks_url: str | None = None
    hs256_secret: str | None = None
    issuer: str | None = None
    audience: str | None = None
    jwks_cache_seconds: int = 300

    @staticmethod
    def jwks(url: str, *, issuer: str | None = None, audience: str | None = None) -> Auth:
        return Auth(jwks_url=url, issuer=issuer, audience=audience)

    @staticmethod
    def hs256(secret: str, *, issuer: str | None = None, audience: str | None = None) -> Auth:
        """Shared-secret tokens: for development and tests. Prefer JWKS in production."""
        return Auth(hs256_secret=secret, issuer=issuer, audience=audience)


@dataclass(frozen=True, slots=True)
class RateLimit:
    """Token bucket: `burst` requests at once (default `per_minute`), refilled at `per_minute`."""

    per_minute: float
    burst: float | None = None


@dataclass(frozen=True, slots=True)
class RateLimits:
    per_device: RateLimit = field(default_factory=lambda: RateLimit(600))
    per_user: RateLimit = field(default_factory=lambda: RateLimit(1800))


@dataclass(frozen=True, slots=True)
class Compaction:
    device_ttl_days: float = 30
    """A device unseen this long is retired and no longer holds compaction back."""
    interval_ms: int = 3_600_000
    """How often a serving process compacts; 0 disables it (the framework packages use it)."""
    min_ops: int = 20
    """Only records with at least this many ops are compacted."""


@dataclass(frozen=True, slots=True)
class Limits:
    max_body_bytes: int = 5 * 1024 * 1024
    max_concurrent_pushes: int = 8
    max_scope_delta: int = 2000
    max_push_ops: int = 500
    max_pull_limit: int = 1000
    max_skew_ms: int = 24 * 3_600_000


@dataclass(frozen=True, slots=True)
class ServerDefinition:
    schema: Schema
    scopes: Mapping[str, ScopeFn]
    access: AccessFn
    auth: Auth
    cors: Sequence[str] = ()
    rate_limit: RateLimits | None = field(default_factory=RateLimits)
    """`None` disables rate limiting (e.g. behind a proxy that limits already)."""
    compaction: Compaction = field(default_factory=Compaction)
    limits: Limits = field(default_factory=Limits)


def define_server(
    *,
    schema: Schema,
    scopes: Mapping[str, ScopeFn],
    access: AccessFn,
    auth: Auth,
    cors: Sequence[str] = (),
    rate_limit: RateLimits | bool | None = True,
    compaction: Compaction | None = None,
    limits: Limits | None = None,
) -> ServerDefinition:
    """Declares an Accord server. Checked at once: every record type needs a scope function."""
    for type_ in schema:
        if not callable(scopes.get(type_)):
            raise ValueError(f'define_server: no scope function for record type "{type_}"')
    if not callable(access):
        raise ValueError("define_server: access must be a function of the claims")
    if not isinstance(auth, Auth) or (auth.jwks_url is None) == (auth.hs256_secret is None):
        raise ValueError("define_server: auth needs jwks_url or hs256_secret (exactly one)")
    if isinstance(cors, str):
        raise ValueError("define_server: cors is a list of origins")
    if rate_limit is True:
        rl: RateLimits | None = RateLimits()
    elif rate_limit is False or rate_limit is None:
        rl = None
    else:
        rl = rate_limit
    if rl is not None:
        for name, r in (("per_device", rl.per_device), ("per_user", rl.per_user)):
            if not (_number(r.per_minute) and r.per_minute > 0):
                raise ValueError(f"define_server: rate_limit.{name}.per_minute must be > 0")
            if r.burst is not None and not (_number(r.burst) and r.burst > 0):
                raise ValueError(f"define_server: rate_limit.{name}.burst must be > 0")
    lim = limits or Limits()
    for name in (
        "max_body_bytes",
        "max_concurrent_pushes",
        "max_scope_delta",
        "max_push_ops",
        "max_pull_limit",
        "max_skew_ms",
    ):
        v = getattr(lim, name)
        if type(v) is not int or v < (0 if name == "max_scope_delta" else 1):
            raise ValueError(f"define_server: limits.{name} must be a positive integer")
    comp = compaction or Compaction()
    if not (_number(comp.device_ttl_days) and comp.device_ttl_days > 0):
        raise ValueError("define_server: compaction.device_ttl_days must be > 0")
    if type(comp.min_ops) is not int or comp.min_ops < 1:
        raise ValueError("define_server: compaction.min_ops must be an integer >= 1")
    return ServerDefinition(
        schema=schema,
        scopes=dict(scopes),
        access=access,
        auth=auth,
        cors=tuple(cors),
        rate_limit=rl,
        compaction=comp,
        limits=lim,
    )


def _number(x: object) -> bool:
    return type(x) is not bool and isinstance(x, int | float)
