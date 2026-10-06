from __future__ import annotations

from accordsync_server import RateLimit, RateLimiter


def test_token_bucket_like_ratelimit_ts() -> None:
    t = [0.0]
    limiter = RateLimiter(RateLimit(per_minute=60, burst=2), now=lambda: t[0])
    assert limiter.take("a") == 0
    assert limiter.take("a") == 0
    assert limiter.take("a") == 1000  # one token per second
    assert limiter.take("b") == 0  # keys are independent
    t[0] = 500
    assert limiter.take("a") == 500
    t[0] = 1000
    assert limiter.take("a") == 0


def test_burst_defaults_to_per_minute_and_full_buckets_are_evicted() -> None:
    t = [0.0]
    limiter = RateLimiter(RateLimit(per_minute=3), now=lambda: t[0], max_keys=2)
    assert [limiter.take("a") for _ in range(4)] == [0, 0, 0, 20_000]
    limiter.take("b")
    t[0] = 120_000
    limiter.take("c")  # evicts the refilled buckets
    assert limiter.take("a") == 0
