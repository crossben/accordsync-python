"""Per-field state for the four strategies.

Each strategy's `apply_op` is commutative and associative over distinct ops: any delivery order of
the same set of ops yields the same state. The replica guarantees each op is applied at most once
(by op id), which makes the whole merge idempotent.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import cmp_to_key
from typing import Any, ClassVar, Final, Literal, TypeAlias

from ._text import utf16_key
from .errors import AccordError
from .hlc import Hlc, compare_hlc
from .op import (
    AddOp,
    AssignOp,
    IncOp,
    JsonValue,
    Op,
    OpId,
    RemoveOp,
    SetElement,
    as_increment,
    as_set_element,
    is_number,
    op_id_key,
)
from .schema import StrategyName


@dataclass(slots=True)
class LwwState:
    winner: AssignOp | None = None
    strategy: ClassVar[Literal["lww"]] = "lww"


@dataclass(slots=True)
class CounterState:
    total: int = 0
    strategy: ClassVar[Literal["counter"]] = "counter"


@dataclass(slots=True)
class SetState:
    tags: dict[OpId, SetElement] = field(default_factory=dict)
    """tag (the op id of the add) -> element"""
    removed: set[OpId] = field(default_factory=set)
    strategy: ClassVar[Literal["set"]] = "set"


@dataclass(slots=True)
class ConflictState:
    live: dict[OpId, JsonValue] = field(default_factory=dict)
    superseded: set[OpId] = field(default_factory=set)
    strategy: ClassVar[Literal["conflict"]] = "conflict"


FieldState: TypeAlias = LwwState | CounterState | SetState | ConflictState

FieldSnapshot: TypeAlias = dict[str, Any]
"""A field's live state as JSON, the shape of the TypeScript `FieldSnapshot`."""


class _Absent:
    """The value of a field never written (`undefined` in TypeScript): left out of reads."""

    _instance: _Absent | None = None

    def __new__(cls) -> _Absent:
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __repr__(self) -> str:
        return "ABSENT"

    def __bool__(self) -> bool:
        return False


ABSENT: Final = _Absent()
"""Read of a `lww` or `conflict` field that has no value. A field assigned `None` reads `None`."""


def empty_state(strategy: StrategyName) -> FieldState:
    if strategy == "lww":
        return LwwState()
    if strategy == "counter":
        return CounterState()
    if strategy == "set":
        return SetState()
    return ConflictState()


def apply_op(state: FieldState, op: Op) -> None:
    """Applies `op` to `state` in place. The op must already be validated against the schema."""
    match state, op:
        case LwwState(), AssignOp():
            w = state.winner
            if w is None or compare_hlc(op.hlc, w.hlc) > 0:
                state.winner = op
        case CounterState(), IncOp():
            state.total += op.by
        case SetState(), AddOp() | RemoveOp():
            # The tags the writer saw are gone for good, even if their adds arrive later.
            for tag in op.deps:
                state.removed.add(tag)
                state.tags.pop(tag, None)
            if isinstance(op, AddOp) and op.op_id not in state.removed:
                state.tags[op.op_id] = op.element
        case ConflictState(), AssignOp():
            for dep in op.deps:
                state.superseded.add(dep)
                state.live.pop(dep, None)
            if op.op_id not in state.superseded:
                state.live[op.op_id] = op.value
        case _:
            raise AccordError(f'op kind "{op.kind}" does not apply to a {state.strategy} field')


def read_state(state: FieldState) -> object:
    """A field as the app sees it, or `ABSENT` for a `lww` or `conflict` field never written.

    `lww`: the value; `counter`: an int; `set`: a sorted list; `conflict`: `{"value": v}` or
    `{"conflicted": [{"value": v, "opId": id}, ...]}` sorted by op id.
    """
    match state:
        case LwwState():
            return ABSENT if state.winner is None else state.winner.value
        case CounterState():
            return state.total
        case SetState():
            # A Python set dedupes like JavaScript's Set: 1 and 1.0 are one element, "1" another.
            return sorted(set(state.tags.values()), key=cmp_to_key(compare_elements))
        case ConflictState():
            live = sorted(state.live.items(), key=lambda kv: op_id_key(kv[0]))
            if not live:
                return ABSENT
            if len(live) == 1:
                return {"value": live[0][1]}
            return {"conflicted": [{"value": v, "opId": k} for k, v in live]}


def observed_deps(state: FieldState, element: SetElement | None = None) -> list[OpId]:
    """Op ids a writer must cite in `deps`: live conflict values, or the tags of a set element."""
    if isinstance(state, ConflictState):
        return sorted(state.live, key=op_id_key)
    if isinstance(state, SetState):
        return sorted(
            (tag for tag, e in state.tags.items() if same_element(e, element)), key=op_id_key
        )
    return []


def same_element(a: object, b: object) -> bool:
    """JavaScript's `===` on set elements: `1` and `1.0` are equal; `"1"` and `1` are not."""
    if isinstance(a, str) or isinstance(b, str):
        return isinstance(a, str) and isinstance(b, str) and a == b
    return is_number(a) and is_number(b) and a == b


def compare_elements(a: SetElement, b: SetElement) -> int:
    """Numbers first (ascending), then strings (by UTF-16 code unit)."""
    if isinstance(a, str) != isinstance(b, str):
        return 1 if isinstance(a, str) else -1
    if isinstance(a, str) and isinstance(b, str):
        ka, kb = utf16_key(a), utf16_key(b)
        return -1 if ka < kb else 1 if ka > kb else 0
    assert not isinstance(a, str)
    assert not isinstance(b, str)
    return -1 if a < b else 1 if a > b else 0


def snapshot_state(state: FieldState) -> FieldSnapshot:
    """A field's live state, as JSON.

    Tombstones (removed set tags, superseded conflict values) are dropped: on the server feed every
    op comes after the ops its `deps` name, so nothing a tombstone guards against can arrive after a
    snapshot (ADR-0008).
    """
    match state:
        case LwwState():
            w = state.winner
            return {
                "strategy": "lww",
                "winner": None
                if w is None
                else {"opId": w.op_id, "hlc": w.hlc.encode(), "value": w.value},
            }
        case CounterState():
            return {"strategy": "counter", "total": state.total}
        case SetState():
            tags = sorted(state.tags.items(), key=lambda kv: op_id_key(kv[0]))
            return {"strategy": "set", "tags": [[k, v] for k, v in tags]}
        case ConflictState():
            live = sorted(state.live.items(), key=lambda kv: op_id_key(kv[0]))
            return {"strategy": "conflict", "live": [[k, v] for k, v in live]}


def state_from_snapshot(snap: FieldSnapshot) -> FieldState:
    try:
        match snap.get("strategy"):
            case "lww":
                w = snap["winner"]
                if w is None:
                    return LwwState()
                hlc = Hlc.decode(w["hlc"])
                return LwwState(AssignOp(w["opId"], "", "", hlc, w["value"], ()))
            case "counter":
                return CounterState(as_increment(snap["total"]))
            case "set":
                return SetState({str(k): as_set_element(e) for k, e in snap["tags"]})
            case "conflict":
                return ConflictState({str(k): v for k, v in snap["live"]})
    except (KeyError, TypeError, ValueError) as e:
        raise AccordError(f"malformed field snapshot: {e}") from None
    raise AccordError(f"unknown snapshot strategy {snap.get('strategy')!r}")
