"""Port of `hlc.test.ts`, with seeded random inputs in place of fast-check."""

import random

import pytest

from accordsync_core import MAX_COUNTER, MAX_SAFE_INTEGER, AccordError, ClockSkewError, Hlc

rnd = random.Random(42)


def random_hlc(node: str | None = None) -> Hlc:
    return Hlc(
        rnd.randrange(2**32) * 900 + rnd.randrange(900),
        rnd.randrange(MAX_COUNTER + 1),
        node or f"n{rnd.randrange(1000)}",
    )


def test_round_trips_through_its_wire_encoding() -> None:
    for _ in range(500):
        h = random_hlc()
        assert Hlc.decode(h.encode()) == h


def test_encodes_in_the_documented_format() -> None:
    assert Hlc(1727871000123, 4, "dev-7f3a").encode() == "1727871000123:00004:dev-7f3a"


@pytest.mark.parametrize(
    "bad",
    [
        "",
        "1:2",
        "x:00001:a",
        "1:00001:",
        "-1:00001:a",
        "1:00001:a:b",
        "1:00001:a\n",  # Python's `$` would accept the newline
        "١:00001:a",  # Arabic-Indic digit: `\d` in Python, not in JavaScript
        "99999999999999999:00001:a",  # 17 digits
        f"{MAX_SAFE_INTEGER + 1}:00001:a",
    ],
)
def test_rejects_malformed_encodings(bad: str) -> None:
    with pytest.raises(AccordError):
        Hlc.decode(bad)


def test_orders_totally_and_antisymmetrically() -> None:
    for i in range(500):
        a = random_hlc()
        b = random_hlc() if i % 2 == 0 else Hlc(a.wall, a.counter, "z")
        assert a.compare(b) == -b.compare(a)
        if a.compare(b) == 0:
            assert a == b


def test_tick_is_strictly_increasing_even_when_the_wall_clock_goes_backwards() -> None:
    h = Hlc.initial("a")
    for _ in range(1000):
        nxt = h.tick(rnd.randrange(10000))
        assert nxt.compare(h) > 0
        h = nxt


def test_receive_moves_past_both_the_local_and_the_remote_clock() -> None:
    for _ in range(500):
        local, remote = random_hlc("local"), random_hlc()
        nxt = local.receive(remote, rnd.randrange(2**32) * 900, MAX_SAFE_INTEGER)
        assert nxt.compare(local) > 0
        assert nxt.wall > remote.wall or (nxt.wall == remote.wall and nxt.counter > remote.counter)
        assert nxt.node == "local"


def test_a_full_counter_rolls_into_the_next_millisecond_instead_of_failing() -> None:
    assert Hlc(5, MAX_COUNTER, "a").tick(0) == Hlc(6, 0, "a")
    assert Hlc.initial("a").receive(Hlc(5, MAX_COUNTER, "b"), 0, 1000) == Hlc(6, 0, "a")


def test_refuses_a_remote_clock_too_far_in_the_future() -> None:
    local = Hlc.initial("a")
    remote = Hlc(10000000, 0, "b")
    with pytest.raises(ClockSkewError):
        local.receive(remote, 1000, 60000)
    local.receive(remote, 9990000, 60000)


@pytest.mark.parametrize("node", ["", "a b", "x" * 65, "é", "a\n"])
def test_refuses_bad_node_ids(node: str) -> None:
    with pytest.raises(AccordError):
        Hlc.initial(node)
