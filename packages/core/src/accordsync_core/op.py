"""Operations: one change to one field of one record."""

from __future__ import annotations

import math
import re
from dataclasses import dataclass
from typing import ClassVar, Literal, TypeAlias

from ._text import utf16_key, utf16_len
from .errors import AccordError
from .hlc import MAX_SAFE_INTEGER, Hlc

JsonValue: TypeAlias = "bool | int | float | str | list[JsonValue] | dict[str, JsonValue] | None"
"""Any JSON value. Field values must survive a JSON round trip."""

SetElement: TypeAlias = str | int | float
"""Set elements are compared by value, so they are limited to strings and finite numbers."""

OpId: TypeAlias = str
"""`deviceId:sequence`. Unique per op, so applying an op twice is a no-op."""

OpKind: TypeAlias = Literal["assign", "inc", "add", "remove"]

_OP_ID = re.compile(r"([A-Za-z0-9_-]{1,64}):([1-9][0-9]{0,15})")
_RECORD_ID = re.compile(r"([A-Za-z][A-Za-z0-9_]{0,63}):(.+)", re.DOTALL)


@dataclass(frozen=True, slots=True)
class AssignOp:
    """Writes a value. For `lww` the highest clock wins. For `conflict`, `deps` lists the values the
    writer could see; the assign supersedes exactly those, so resolving a conflict is an assign
    whose deps name every conflicting value (ADR-0003)."""

    kind: ClassVar[Literal["assign"]] = "assign"
    op_id: OpId
    record: str
    field: str
    hlc: Hlc
    value: JsonValue
    deps: tuple[OpId, ...] = ()


@dataclass(frozen=True, slots=True)
class IncOp:
    """Adds `by` (a positive or negative integer) to a counter."""

    kind: ClassVar[Literal["inc"]] = "inc"
    op_id: OpId
    record: str
    field: str
    hlc: Hlc
    by: int


@dataclass(frozen=True, slots=True)
class AddOp:
    """Adds an element to a set. The op id is the element's unique tag. `deps` lists the element's
    tags the writer could see: the add replaces them, so re-adding keeps the set's state small,
    while a concurrent remove (which only cites the tags it saw) still loses."""

    kind: ClassVar[Literal["add"]] = "add"
    op_id: OpId
    record: str
    field: str
    hlc: Hlc
    element: SetElement
    deps: tuple[OpId, ...] = ()


@dataclass(frozen=True, slots=True)
class RemoveOp:
    """Removes the add-tags in `deps` (the ones the writer had seen); concurrent adds survive."""

    kind: ClassVar[Literal["remove"]] = "remove"
    op_id: OpId
    record: str
    field: str
    hlc: Hlc
    element: SetElement
    deps: tuple[OpId, ...] = ()


Op: TypeAlias = AssignOp | IncOp | AddOp | RemoveOp


@dataclass(frozen=True, slots=True)
class ParsedOpId:
    device: str
    seq: int


def parse_op_id(op_id: str) -> ParsedOpId:
    m = _OP_ID.fullmatch(op_id) if isinstance(op_id, str) else None
    if not m:
        raise AccordError(f'malformed op id "{op_id}" (expected device:sequence)')
    return ParsedOpId(m[1], int(m[2]))


def record_type(record: str) -> str:
    """The type of a `type:id` record id (the id: 1 to 256 UTF-16 code units, as in JavaScript)."""
    m = _RECORD_ID.fullmatch(record) if isinstance(record, str) else None
    if not m or utf16_len(m[2]) > 256:
        raise AccordError(f'malformed record id "{record}" (expected type:id)')
    return m[1]


def op_id_key(op_id: OpId) -> bytes:
    """Sort key for op ids, used wherever output order must be deterministic (code-unit order)."""
    return utf16_key(op_id)


def compare_op_ids(a: OpId, b: OpId) -> int:
    ka, kb = op_id_key(a), op_id_key(b)
    return -1 if ka < kb else 1 if ka > kb else 0


def is_number(x: object) -> bool:
    """A JSON number: `int` or `float`, never `bool` (which is an `int` subclass in Python)."""
    return type(x) is not bool and isinstance(x, int | float)


def as_set_element(e: object) -> SetElement:
    """Checks a set element: a string or a finite number. Booleans are refused, so `True` can never
    be confused with `1`. An integer past 2^53 becomes the double JavaScript would read."""
    if isinstance(e, str):
        return e
    if type(e) is int:
        return e if abs(e) <= MAX_SAFE_INTEGER else float(e)
    if type(e) is float and math.isfinite(e):
        return e
    raise AccordError("set elements must be a string or finite number")


def as_increment(by: object) -> int:
    """Checks a counter increment: a safe integer. A whole float (`3.0`, as some JSON decoders give)
    is the same number to JavaScript and is accepted; `True` is not a number."""
    if type(by) is float and math.isfinite(by) and by == int(by):
        by = int(by)
    if type(by) is not int or abs(by) > MAX_SAFE_INTEGER:
        raise AccordError(f"counter increment must be a safe integer, got {by!r}")
    return by
