from __future__ import annotations

import pytest
from accordsync_core import counter, define_schema, lww
from accordsync_server import (
    Access,
    Auth,
    Compaction,
    Limits,
    RateLimit,
    RateLimits,
    ScopedRecord,
    define_server,
)

SCHEMA = define_schema({"dossier": {"agent": lww(), "visits": counter()}})


def scopes(r: ScopedRecord) -> list[str]:
    return [f"agent:{r.fields.get('agent')}"]


def access(claims: dict[str, object]) -> Access:
    return Access(read=[f"agent:{claims['sub']}"], write=[f"agent:{claims['sub']}"])


AUTH = Auth.hs256("x" * 32)


def test_defaults_match_the_typescript_server() -> None:
    d = define_server(schema=SCHEMA, scopes={"dossier": scopes}, access=access, auth=AUTH)
    assert d.limits == Limits(5 * 1024 * 1024, 8, 2000, 500, 1000, 86_400_000)
    assert d.rate_limit == RateLimits(RateLimit(600), RateLimit(1800))
    assert d.compaction == Compaction(30, 3_600_000, 20)
    assert d.cors == ()


def test_every_record_type_needs_a_scope_function() -> None:
    with pytest.raises(ValueError, match='no scope function for record type "dossier"'):
        define_server(schema=SCHEMA, scopes={}, access=access, auth=AUTH)
    with pytest.raises(ValueError, match="no scope function"):
        define_server(schema=SCHEMA, scopes={"dossier": "nope"}, access=access, auth=AUTH)  # type: ignore[dict-item]


def test_auth_needs_exactly_one_of_jwks_or_secret() -> None:
    for auth in (Auth(), Auth(jwks_url="https://x/jwks", hs256_secret="s" * 32)):
        with pytest.raises(ValueError, match="auth needs"):
            define_server(schema=SCHEMA, scopes={"dossier": scopes}, access=access, auth=auth)


def test_rate_limit_false_disables_it_and_bad_rates_are_refused() -> None:
    d = define_server(
        schema=SCHEMA, scopes={"dossier": scopes}, access=access, auth=AUTH, rate_limit=False
    )
    assert d.rate_limit is None
    with pytest.raises(ValueError, match="per_minute"):
        define_server(
            schema=SCHEMA,
            scopes={"dossier": scopes},
            access=access,
            auth=AUTH,
            rate_limit=RateLimits(per_device=RateLimit(0)),
        )


@pytest.mark.parametrize(
    "limits",
    [Limits(max_push_ops=0), Limits(max_body_bytes=-1), Limits(max_pull_limit=True)],
)
def test_bad_limits_are_refused(limits: Limits) -> None:
    with pytest.raises(ValueError, match="limits"):
        define_server(
            schema=SCHEMA, scopes={"dossier": scopes}, access=access, auth=AUTH, limits=limits
        )


def test_bad_compaction_settings_are_refused() -> None:
    with pytest.raises(ValueError, match="min_ops"):
        define_server(
            schema=SCHEMA,
            scopes={"dossier": scopes},
            access=access,
            auth=AUTH,
            compaction=Compaction(min_ops=0),
        )
