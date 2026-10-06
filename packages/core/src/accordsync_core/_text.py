"""String helpers that make Python strings behave like JavaScript's UTF-16 strings."""


def utf16_key(s: str) -> bytes:
    """Sort key giving JavaScript's `a < b` order: by UTF-16 code unit, not by code point.

    The two differ for characters above U+FFFF (surrogate pairs, 0xD800...) against U+E000-U+FFFF.
    `surrogatepass` keeps lone surrogates, which JSON may carry and JavaScript strings may hold.
    """
    return s.encode("utf-16-be", "surrogatepass")


def utf16_len(s: str) -> int:
    """`s.length` in JavaScript: the number of UTF-16 code units."""
    return len(utf16_key(s)) // 2
