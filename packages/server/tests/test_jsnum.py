from __future__ import annotations

import pytest
from accordsync_server.jsnum import js_safe_integer


@pytest.mark.parametrize(
    ("text", "value"),
    [
        ("0", 0),
        ("", 0),
        (" 12 ", 12),
        ("1e3", 1000),
        ("0x10", 16),
        ("1.0", 1),
        ("-5", -5),
        ("1.5", None),
        ("abc", None),
        ("9007199254740992", None),
        ("Infinity", None),
        ("1_000", None),
    ],
)
def test_reads_numbers_like_javascript_number(text: str, value: int | None) -> None:
    assert js_safe_integer(text) == value
