"""JavaScript's `Number(string)`, for query parameters and op numbers read as the TypeScript
server reads them."""

from __future__ import annotations

import math
import re

# JavaScript's whitespace and line terminators, which `Number()` trims.
_WS = " \t\n\v\f\r\u00a0\u1680\u2028\u2029\u202f\u205f\u3000\ufeff" + "".join(
    chr(c) for c in range(0x2000, 0x200B)
)
_DECIMAL = re.compile(r"[+-]?(?:[0-9]+\.?[0-9]*|\.[0-9]+)(?:[eE][+-]?[0-9]+)?")
_RADIX = re.compile(r"0([xXoObB])([0-9a-fA-F]+)")
_BASES = {"x": 16, "o": 8, "b": 2}


def js_number_from_string(s: str) -> float | None:
    """`Number(s)`, or None for NaN. Infinities come back as floats."""
    t = s.strip(_WS)
    if t == "":
        return 0.0
    if t in ("Infinity", "+Infinity"):
        return math.inf
    if t == "-Infinity":
        return -math.inf
    m = _RADIX.fullmatch(t)
    if m:
        try:
            return float(int(m[2], _BASES[m[1].lower()]))
        except ValueError:
            return None
    if _DECIMAL.fullmatch(t):
        return float(t)
    return None


def js_safe_integer(s: str) -> int | None:
    """`Number(s)` when `Number.isSafeInteger` of it, else None."""
    n = js_number_from_string(s)
    if n is None or not math.isfinite(n) or n != int(n) or abs(n) > 2**53 - 1:
        return None
    return int(n)
