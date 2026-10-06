"""Canonical JSON, byte for byte what the TypeScript core's `canonicalJson` prints.

That is `JSON.stringify` of objects built from code-unit-sorted keys, so this reproduces
JavaScript rather than Python's `json` module:
- keys that are array indices ("0".."4294967294", no leading zeros) come first in numeric order,
  because JavaScript objects always order them that way; the others follow by UTF-16 code unit;
- numbers print as JavaScript prints them: `1` not `1.0`, `0` for `-0.0`, `1e-7`, `1e+21`;
- strings escape exactly like `JSON.stringify` (lone surrogates as `\\udXXX`, the rest raw).
"""

import math
import re
from collections.abc import Iterable, Mapping
from decimal import Decimal

from ._text import utf16_key

MAX_ARRAY_INDEX = 4294967294
_ARRAY_INDEX = re.compile(r"0|[1-9][0-9]*")
_SHORT_ESCAPES = {'"': '\\"', "\\": "\\\\", "\b": "\\b", "\f": "\\f", "\n": "\\n", "\r": "\\r"}
_SHORT_ESCAPES["\t"] = "\\t"


def canonical_json(value: object) -> str:
    out: list[str] = []
    _write(out, value)
    return "".join(out)


def _write(out: list[str], value: object) -> None:
    # `bool` first: it is a subclass of `int`.
    if value is None:
        out.append("null")
    elif type(value) is bool:
        out.append("true" if value else "false")
    elif isinstance(value, int | float):
        out.append(js_number(value))
    elif isinstance(value, str):
        out.append(js_string(value))
    elif isinstance(value, list | tuple):
        out.append("[")
        for i, v in enumerate(value):
            if i:
                out.append(",")
            _write(out, v)
        out.append("]")
    elif isinstance(value, Mapping):
        out.append("{")
        for i, k in enumerate(js_key_order(value.keys())):
            if i:
                out.append(",")
            out.append(js_string(k))
            out.append(":")
            _write(out, value[k])
        out.append("}")
    else:
        raise TypeError(f"canonical_json: not a JSON value: {type(value).__name__}")


def js_key_order(keys: Iterable[object]) -> list[str]:
    """The order JavaScript gives the keys of an object built from code-unit-sorted entries."""
    indices: list[str] = []
    others: list[str] = []
    for k in keys:
        if not isinstance(k, str):
            raise TypeError(f"canonical_json: object keys must be strings, got {k!r}")
        if _ARRAY_INDEX.fullmatch(k) and int(k) <= MAX_ARRAY_INDEX:
            indices.append(k)
        else:
            others.append(k)
    indices.sort(key=int)
    others.sort(key=utf16_key)
    return indices + others


def js_number(n: int | float) -> str:
    """A number as JavaScript's `String(n)` / `JSON.stringify` prints it."""
    if isinstance(n, int):
        # JavaScript numbers are doubles: an integer past 2^53 prints as the double it rounds to.
        if abs(n) <= 2**53:
            return str(n)
        n = float(n)
    if not math.isfinite(n):
        return "null"
    if n == 0:
        return "0"  # also -0.0
    sign = "-" if n < 0 else ""
    # repr gives the shortest digits that round-trip, as JavaScript does; only the layout differs.
    # The value is 0.DIGITS x 10^point (the k digits and n of ECMAScript's Number::toString).
    t = Decimal(repr(abs(n))).normalize().as_tuple()
    digits = "".join(map(str, t.digits))
    point = len(digits) + int(t.exponent)
    k = len(digits)
    if k <= point <= 21:
        return sign + digits + "0" * (point - k)
    if 0 < point <= 21:
        return sign + digits[:point] + "." + digits[point:]
    if -6 < point <= 0:
        return sign + "0." + "0" * -point + digits
    e = point - 1
    e_str = f"e+{e}" if e >= 0 else f"e-{-e}"
    if k == 1:
        return sign + digits + e_str
    return sign + digits[0] + "." + digits[1:] + e_str


def js_string(s: str) -> str:
    """A string as `JSON.stringify` prints it."""
    # Re-pair surrogates first: a high and a low surrogate side by side are one character to
    # JavaScript, printed raw; only lone surrogates are escaped.
    s = s.encode("utf-16-be", "surrogatepass").decode("utf-16-be", "surrogatepass")
    out = ['"']
    for ch in s:
        c = ord(ch)
        if ch in _SHORT_ESCAPES:
            out.append(_SHORT_ESCAPES[ch])
        elif c < 0x20 or 0xD800 <= c <= 0xDFFF:
            out.append(f"\\u{c:04x}")
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)
