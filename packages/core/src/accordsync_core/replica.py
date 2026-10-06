"""An op log and the state projected from it."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any, Literal, TypeAlias

from ._text import utf16_key
from .canonical import canonical_json
from .errors import AccordError
from .op import Op, OpId, SetElement, op_id_key, parse_op_id
from .schema import KINDS, Schema, fields_of, strategy_for
from .strategies import (
    ABSENT,
    ConflictState,
    FieldSnapshot,
    FieldState,
    apply_op,
    empty_state,
    observed_deps,
    read_state,
    snapshot_state,
    state_from_snapshot,
)

ApplyResult: TypeAlias = Literal["applied", "duplicate"]


@dataclass(frozen=True, slots=True)
class RecordSnapshot:
    """A record's state with its history folded away (log compaction, ADR-0008)."""

    record: str
    fields: Mapping[str, FieldSnapshot]

    def to_json(self) -> dict[str, Any]:
        return {"record": self.record, "fields": dict(self.fields)}

    @staticmethod
    def from_json(data: Mapping[str, Any]) -> RecordSnapshot:
        record, fields = data.get("record"), data.get("fields")
        if not isinstance(record, str) or not isinstance(fields, Mapping):
            raise AccordError("record snapshot needs a record and fields")
        return RecordSnapshot(record, dict(fields))


@dataclass(frozen=True, slots=True)
class FieldRef:
    record: str
    field: str


class Replica:
    """An op log and the state projected from it. Pure: no I/O, no clock.

    Two replicas holding the same set of ops always read the same state, whatever order the ops
    arrived in.
    """

    def __init__(self, schema: Schema) -> None:
        self._schema = schema
        self._ops: dict[OpId, Op] = {}
        self._records: dict[str, dict[str, FieldState]] = {}
        # Snapshots this replica's state was started from, by record.
        self._bases: dict[str, RecordSnapshot] = {}

    @property
    def schema(self) -> Schema:
        return self._schema

    def has(self, op_id: OpId) -> bool:
        return op_id in self._ops

    def __len__(self) -> int:
        return len(self._ops)

    @property
    def size(self) -> int:
        return len(self._ops)

    def ops(self) -> list[Op]:
        """All ops, in a deterministic order."""
        return sorted(self._ops.values(), key=lambda o: op_id_key(o.op_id))

    def validate(self, op: Op) -> None:
        """Raises (without changing anything) if the op does not fit the schema."""
        strategy = strategy_for(self._schema, op.record, op.field)
        if op.kind not in KINDS[strategy]:
            raise AccordError(
                f'op kind "{op.kind}" does not apply to {op.field}, a {strategy} field'
            )
        if parse_op_id(op.op_id).device != op.hlc.node:
            raise AccordError(f'op {op.op_id} carries a clock from "{op.hlc.node}"')

    def apply(self, op: Op) -> ApplyResult:
        if op.op_id in self._ops:
            return "duplicate"
        self.validate(op)
        apply_op(self._state(op.record, op.field), op)
        self._ops[op.op_id] = op
        return "applied"

    def read(self, record: str) -> dict[str, object] | None:
        """The record's fields, or None if no op touched it. Fields never written are left out."""
        states = self._records.get(record)
        if states is None:
            return None
        out: dict[str, object] = {}
        for name, s in fields_of(self._schema, record).items():
            state = states.get(name)
            value = read_state(state if state is not None else empty_state(s.strategy))
            if value is not ABSENT:
                out[name] = value
        return out

    def records(self) -> list[str]:
        return sorted(self._records, key=utf16_key)

    def conflicts(self) -> list[FieldRef]:
        """Every `conflict()` field currently holding more than one value."""
        out: list[FieldRef] = []
        for record in self.records():
            states = self._records[record]
            for name in sorted(states, key=utf16_key):
                state = states[name]
                if isinstance(state, ConflictState) and len(state.live) > 1:
                    out.append(FieldRef(record, name))
        return out

    def observed_deps(
        self, record: str, field: str, element: SetElement | None = None
    ) -> list[OpId]:
        """Op ids a new write to this field must cite (see `AssignOp` and `RemoveOp`)."""
        strategy_for(self._schema, record, field)
        state = self._records.get(record, {}).get(field)
        return observed_deps(state, element) if state is not None else []

    def snapshot(self) -> str:
        """The whole state as canonical JSON: equal strings mean converged replicas."""
        return canonical_json({r: self.read(r) for r in self.records()})

    def snapshot_record(self, record: str) -> RecordSnapshot:
        """The record's current state, with its history folded away."""
        states = self._records.get(record, {})
        return RecordSnapshot(record, {f: snapshot_state(s) for f, s in states.items()})

    def load_snapshot(self, snap: RecordSnapshot, keep: Iterable[OpId] = ()) -> None:
        """Replaces a record's state with a snapshot and forgets that record's ops, except `keep`
        (local ops not yet on the server), which are applied again on top."""
        keep = set(keep)
        fields: dict[str, FieldState] = {}
        for name, fs in snap.fields.items():
            strategy_for(self._schema, snap.record, name)
            fields[name] = state_from_snapshot(fs)
        # Everything checked: now change state.
        reapply = [o for o in self._ops.values() if o.record == snap.record and o.op_id in keep]
        self._ops = {k: o for k, o in self._ops.items() if o.record != snap.record}
        self._records[snap.record] = fields
        self._bases[snap.record] = snap
        for op in reapply:
            self.apply(op)

    def without(self, drop: Iterable[OpId]) -> Replica:
        """A copy without the given ops (same snapshots, every other op): used to roll back."""
        drop = set(drop)
        nxt = Replica(self._schema)
        for snap in self._bases.values():
            nxt.load_snapshot(snap)
        for op in self.ops():
            if op.op_id not in drop:
                nxt.apply(op)
        return nxt

    def forget(self, record: str, keep: Iterable[OpId] = ()) -> Replica:
        """Forgets a record entirely (it left this device's scope), except ops in `keep`."""
        keep = set(keep)
        nxt = Replica(self._schema)
        for r, snap in self._bases.items():
            if r != record:
                nxt.load_snapshot(snap)
        for op in self.ops():
            if op.record != record or op.op_id in keep:
                nxt.apply(op)
        return nxt

    def bases(self) -> list[RecordSnapshot]:
        """Snapshots this replica was started from (to persist them alongside the ops)."""
        return list(self._bases.values())

    def _state(self, record: str, field: str) -> FieldState:
        fields = self._records.setdefault(record, {})
        state = fields.get(field)
        if state is None:
            state = fields[field] = empty_state(strategy_for(self._schema, record, field))
        return state
