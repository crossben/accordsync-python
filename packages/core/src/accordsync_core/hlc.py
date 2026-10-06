"""Hybrid logical clock: physical time + logical counter + node id.

Orders events consistently even when device clocks are wrong, and `compare_hlc` is a total order:
two distinct clocks never compare equal, because the node id breaks ties.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from ._text import utf16_key
from .errors import AccordError, ClockSkewError

MAX_COUNTER = 99_999
MAX_SAFE_INTEGER = 2**53 - 1
"""JavaScript's `Number.MAX_SAFE_INTEGER`: integers on the wire stay within +/- this."""

# fullmatch and explicit [0-9]: Python's `$` accepts a trailing newline and `\d` any Unicode digit.
_NODE = re.compile(r"[A-Za-z0-9_-]{1,64}")
_ENCODED = re.compile(r"([0-9]{1,16}):([0-9]{5}):([A-Za-z0-9_-]{1,64})")


@dataclass(frozen=True, slots=True)
class Hlc:
    wall: int
    """Milliseconds since the Unix epoch, as seen by the node (maybe pushed forward by others)."""
    counter: int
    """Disambiguates events within the same `wall` millisecond."""
    node: str
    """The device or server that produced the clock."""

    @staticmethod
    def initial(node: str) -> Hlc:
        assert_node(node)
        return Hlc(0, 0, node)

    def encode(self) -> str:
        """`wall:counter:node`, with the counter zero-padded to 5 digits."""
        return f"{self.wall}:{self.counter:05d}:{self.node}"

    @staticmethod
    def decode(s: str) -> Hlc:
        m = _ENCODED.fullmatch(s)
        if not m:
            raise AccordError(f'malformed hlc "{s}"')
        wall = int(m[1])
        if wall > MAX_SAFE_INTEGER:
            raise AccordError(f'hlc wall out of range in "{s}"')
        return Hlc(wall, int(m[2]), m[3])

    def compare(self, other: Hlc) -> int:
        return compare_hlc(self, other)

    def tick(self, now: int) -> Hlc:
        """The clock for a new local event at physical time `now`."""
        if now > self.wall:
            return Hlc(now, 0, self.node)
        return _after(self.wall, self.counter, self.node)

    def receive(self, remote: Hlc, now: int, max_skew_ms: int) -> Hlc:
        """The clock after observing `remote` at physical time `now`.

        Refuses a remote clock more than `max_skew_ms` ahead of `now`, so one phone with a wrong
        date cannot win every merge forever.
        """
        if remote.wall - now > max_skew_ms:
            raise ClockSkewError(
                f"clock of {remote.node} is {remote.wall - now} ms ahead (limit {max_skew_ms} ms)"
            )
        wall = max(self.wall, remote.wall, now)
        if wall == self.wall and wall == remote.wall:
            return _after(wall, max(self.counter, remote.counter), self.node)
        if wall == self.wall:
            return _after(wall, self.counter, self.node)
        if wall == remote.wall:
            return _after(wall, remote.counter, self.node)
        return Hlc(wall, 0, self.node)


def compare_hlc(a: Hlc, b: Hlc) -> int:
    if a.wall != b.wall:
        return -1 if a.wall < b.wall else 1
    if a.counter != b.counter:
        return -1 if a.counter < b.counter else 1
    if a.node != b.node:
        return -1 if utf16_key(a.node) < utf16_key(b.node) else 1
    return 0


def assert_node(node: str) -> None:
    if not isinstance(node, str) or not _NODE.fullmatch(node):
        raise AccordError(f'node id must match [A-Za-z0-9_-]{{1,64}}, got "{node}"')


def _after(wall: int, counter: int, node: str) -> Hlc:
    """The smallest clock after (wall, counter): a full counter rolls into the next millisecond."""
    if counter >= MAX_COUNTER:
        return Hlc(wall + 1, 0, node)
    return Hlc(wall, counter + 1, node)
