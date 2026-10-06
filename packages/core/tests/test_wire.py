"""Port of `wire.test.ts`."""

import json
from typing import Any

import pytest

from accordsync_core import (
    MAX_SAFE_INTEGER,
    AccordError,
    AddOp,
    AssignOp,
    Hlc,
    IncOp,
    canonical_json,
    decode_op,
    encode_op,
    parse_op_id,
    record_type,
)

OP = AssignOp(
    "dev-7f3a:1042",
    "dossier:91",
    "status",
    Hlc(1727871000123, 4, "dev-7f3a"),
    "submitted",
    ("dev-7f3a:1041",),
)


def test_round_trips_an_op() -> None:
    back = decode_op(json.loads(json.dumps(encode_op(OP))))
    assert back == OP
    assert encode_op(back) == encode_op(OP)


def test_matches_the_documented_shape() -> None:
    assert encode_op(OP) == {
        "op_id": "dev-7f3a:1042",
        "record": "dossier:91",
        "field": "status",
        "kind": "assign",
        "value": "submitted",
        "hlc": "1727871000123:00004:dev-7f3a",
        "deps": ["dev-7f3a:1041"],
    }


def test_a_first_add_leaves_deps_out_like_the_typescript_encoder() -> None:
    add = AddOp("a:1", "dossier:1", "docs", Hlc(1, 0, "a"), "x")
    assert "deps" not in encode_op(add)
    assert decode_op(encode_op(add)) == add


def test_accepts_a_whole_number_sent_as_a_float_for_inc() -> None:
    op = decode_op({**encode_op(OP), "kind": "inc", "by": 3.0})
    assert isinstance(op, IncOp)
    assert op.by == 3
    assert type(op.by) is int


def test_null_is_a_value() -> None:
    op = decode_op({**encode_op(OP), "value": None})
    assert isinstance(op, AssignOp)
    assert op.value is None


def test_a_lone_surrogate_survives_the_round_trip() -> None:
    wire = {**encode_op(OP), "value": "\ud800", "record": "dossier:\udfff"}
    text = json.dumps(wire)  # ASCII escapes: no codec ever sees the surrogate
    assert canonical_json(encode_op(decode_op(json.loads(text)))) == canonical_json(wire)


GOOD = encode_op(OP)
NO_VALUE = {k: v for k, v in GOOD.items() if k != "value"}
BAD: list[Any] = [
    None,
    [GOOD],
    "op",
    {**GOOD, "op_id": "no-seq"},
    {**GOOD, "op_id": "dev-7f3a:01"},
    {**GOOD, "op_id": "dev-7f3a:1\n"},
    {**GOOD, "op_id": "other:1"},  # op id device must match the clock's node
    {**GOOD, "record": "no-type"},
    {**GOOD, "record": "dossier:"},
    {**GOOD, "record": "dossier:" + "x" * 257},
    {**GOOD, "record": "dossier:" + "😀" * 129},  # 258 UTF-16 code units
    {**GOOD, "kind": "explode"},
    {**GOOD, "kind": None},
    {**GOOD, "kind": "inc", "by": 1.5},
    {**GOOD, "kind": "inc", "by": True},  # bool is an int in Python, never a number here
    {**GOOD, "kind": "inc", "by": "1"},
    {**GOOD, "kind": "inc", "by": MAX_SAFE_INTEGER + 1},
    {**GOOD, "kind": "inc", "by": float("inf")},
    {**GOOD, "kind": "inc"},
    {**GOOD, "kind": "add", "element": []},
    {**GOOD, "kind": "add", "element": float("nan")},
    {**GOOD, "kind": "add", "element": True},
    {**GOOD, "kind": "add", "element": None},
    {**{k: v for k, v in GOOD.items() if k != "deps"}, "kind": "remove", "element": "x"},
    {**GOOD, "deps": "a:1"},
    {**GOOD, "deps": [1]},
    {**GOOD, "deps": ["nope"]},
    {**GOOD, "hlc": "garbage"},
    {**GOOD, "hlc": 5},
    NO_VALUE,
]


@pytest.mark.parametrize("bad", BAD, ids=range(len(BAD)))
def test_rejects_malformed_ops_with_a_reason(bad: object) -> None:
    with pytest.raises(AccordError):
        decode_op(bad)


def test_safe_integer_bounds_are_inclusive() -> None:
    op = decode_op({**GOOD, "kind": "inc", "by": -MAX_SAFE_INTEGER})
    assert isinstance(op, IncOp)
    assert op.by == -MAX_SAFE_INTEGER


def test_parse_op_id_and_record_type() -> None:
    assert parse_op_id("B_x-1:42").device == "B_x-1"
    assert parse_op_id("B_x-1:42").seq == 42
    assert record_type("dossier:a:b\nc") == "dossier"
    assert record_type("item:" + "😀" * 128) == "item"
