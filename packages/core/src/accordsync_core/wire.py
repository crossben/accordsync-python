"""The wire format: ops as they travel over the network and sit in golden vectors."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from .canonical import canonical_json
from .errors import AccordError
from .hlc import Hlc
from .op import (
    AddOp,
    AssignOp,
    IncOp,
    Op,
    OpId,
    RemoveOp,
    SetElement,
    as_increment,
    as_set_element,
    parse_op_id,
    record_type,
)

WireOp = dict[str, Any]


def encode_op(op: Op) -> WireOp:
    base: WireOp = {
        "op_id": op.op_id,
        "record": op.record,
        "field": op.field,
        "hlc": op.hlc.encode(),
    }
    match op:
        case AssignOp():
            return {**base, "kind": "assign", "value": op.value, "deps": list(op.deps)}
        case IncOp():
            return {**base, "kind": "inc", "by": op.by}
        case AddOp():
            # `deps` is omitted when empty, so first adds keep the v0.1 wire shape.
            if op.deps:
                return {**base, "kind": "add", "element": op.element, "deps": list(op.deps)}
            return {**base, "kind": "add", "element": op.element}
        case RemoveOp():
            return {**base, "kind": "remove", "element": op.element, "deps": list(op.deps)}


def decode_op(data: object) -> Op:
    """Parses untrusted input into an op, or raises `AccordError` with the reason.

    Schema checks happen later (`Replica.validate`).
    """
    if not isinstance(data, Mapping):
        raise AccordError("op must be an object")
    o: Mapping[str, object] = data
    op_id = _str(o, "op_id")
    record = _str(o, "record")
    field = _str(o, "field")
    hlc = Hlc.decode(_str(o, "hlc"))
    device = parse_op_id(op_id).device
    record_type(record)
    if device != hlc.node:
        raise AccordError(f'op {op_id} carries a clock from "{hlc.node}"')
    kind = o.get("kind")
    if kind == "assign":
        # JSON null arrives as None and is a value; only a missing key is refused.
        if "value" not in o:
            raise AccordError("assign needs a value")
        return AssignOp(op_id, record, field, hlc, o["value"], _deps(o))  # type: ignore[arg-type]
    if kind == "inc":
        try:
            by = as_increment(o.get("by"))
        except AccordError:
            raise AccordError('inc needs an integer "by"') from None
        return IncOp(op_id, record, field, hlc, by)
    if kind == "add":
        return AddOp(op_id, record, field, hlc, _element(o), _deps(o) if "deps" in o else ())
    if kind == "remove":
        return RemoveOp(op_id, record, field, hlc, _element(o), _deps(o))
    raise AccordError(f"unknown op kind {_describe(kind)}")


def _describe(v: object) -> str:
    try:
        return canonical_json(v)
    except TypeError:
        return repr(v)


def _str(o: Mapping[str, object], key: str) -> str:
    v = o.get(key)
    if not isinstance(v, str):
        raise AccordError(f'"{key}" must be a string')
    return v


def _deps(o: Mapping[str, object]) -> tuple[OpId, ...]:
    d = o.get("deps")
    if not isinstance(d, list | tuple):
        raise AccordError('"deps" must be an array of op ids')
    for dep in d:
        if not isinstance(dep, str):
            raise AccordError('"deps" must contain strings')
        parse_op_id(dep)
    return tuple(d)


def _element(o: Mapping[str, object]) -> SetElement:
    try:
        return as_set_element(o.get("element"))
    except AccordError:
        raise AccordError('"element" must be a string or finite number') from None
