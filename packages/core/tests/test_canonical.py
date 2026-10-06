"""Canonical JSON must equal what `JSON.stringify` gives in the TypeScript core. The random vectors
cover most of it; these are the cases JSON input can never carry into Python."""

import json
import math

import pytest

from accordsync_core import canonical_json


@pytest.mark.parametrize(
    ("value", "js"),
    [
        (-0.0, "0"),
        (0.0, "0"),
        (1.0, "1"),
        (-2.0, "-2"),
        (0.1, "0.1"),
        (1e21, "1e+21"),
        (1e20, "100000000000000000000"),
        (1e-7, "1e-7"),
        (1.5e-7, "1.5e-7"),
        (1e-6, "0.000001"),
        (123456789.125, "123456789.125"),
        (5e-324, "5e-324"),
        (-1.5e300, "-1.5e+300"),
        (2**53 - 1, "9007199254740991"),
        (2**60, "1152921504606847000"),  # a JavaScript number is a double
        (math.nan, "null"),
        (math.inf, "null"),
        (-math.inf, "null"),
        (True, "true"),
        (False, "false"),
        (None, "null"),
    ],
)
def test_numbers_print_as_javascript_prints_them(value: object, js: str) -> None:
    assert canonical_json(value) == js


def test_floats_round_trip_with_the_shortest_digits() -> None:
    for x in [0.1 + 0.2, 1 / 3, 2.5e-8, 9.999999999999999e22, 1.7976931348623157e308]:
        assert float(canonical_json(x)) == x
        assert canonical_json(x) == canonical_json(json.loads(canonical_json(x)))


def test_keys_array_indices_first_in_numeric_order_then_code_unit_order() -> None:
    value = {"b": 1, "a": 2, "10": 3, "9": 4, "01": 5, "4294967295": 6, "B": 7, "4294967294": 8}
    assert canonical_json(value) == (
        '{"9":4,"10":3,"4294967294":8,"01":5,"4294967295":6,"B":7,"a":2,"b":1}'
    )


def test_keys_astral_characters_sort_before_the_private_use_area() -> None:
    # By code point U+E000 < U+1F600; by UTF-16 code unit 0xD83D < 0xE000.
    assert canonical_json({"": 1, "😀": 2, "-1": 3, "1.5": 4}) == ('{"-1":3,"1.5":4,"😀":2,"":1}')


def test_strings_escape_like_json_stringify() -> None:
    assert canonical_json('\x00\x1f\x7f\b\f\n\r\t"\\ /é😀  ') == (
        '"\\u0000\\u001f\x7f\\b\\f\\n\\r\\t\\"\\\\ /é😀  "'
    )


def test_lone_surrogates_are_escaped_and_pairs_kept() -> None:
    assert canonical_json("\ud800") == '"\\ud800"'
    assert canonical_json("a\udfffb") == '"a\\udfffb"'
    # A high and a low surrogate side by side are one character to JavaScript.
    assert canonical_json("😀") == '"😀"'


def test_nesting_and_tuples() -> None:
    assert canonical_json({"z": [1, (2.0, {"y": None, "x": []})], "a": {}}) == (
        '{"a":{},"z":[1,[2,{"x":[],"y":null}]]}'
    )


@pytest.mark.parametrize("value", [{1, 2}, object(), {1: "x"}, b"x"])
def test_refuses_what_is_not_json(value: object) -> None:
    with pytest.raises(TypeError):
        canonical_json(value)
