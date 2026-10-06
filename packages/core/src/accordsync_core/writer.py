"""One device's replica plus the means to write to it."""

from __future__ import annotations

from collections.abc import Callable, Iterable
from typing import TypeVar

from .errors import AccordError
from .hlc import Hlc, assert_node
from .op import (
    AddOp,
    AssignOp,
    IncOp,
    JsonValue,
    Op,
    OpId,
    RemoveOp,
    as_increment,
    as_set_element,
    parse_op_id,
)
from .replica import ApplyResult, Replica
from .schema import Schema

_O = TypeVar("_O", AssignOp, IncOp, AddOp, RemoveOp)

DEFAULT_MAX_SKEW_MS = 24 * 60 * 60 * 1000


class LocalWriter:
    """Every local write becomes an op, applied locally first and returned so the caller can queue
    it for sync.

    `now` gives physical time in ms: injected, the core never reads the clock itself. Remote clocks
    further ahead than `max_skew_ms` are refused. `resume` restores a device's last clock and
    op sequence number.
    """

    def __init__(
        self,
        schema: Schema,
        device_id: str,
        now: Callable[[], int],
        max_skew_ms: int = DEFAULT_MAX_SKEW_MS,
        resume: tuple[Hlc, int] | None = None,
    ) -> None:
        assert_node(device_id)
        self.device_id = device_id
        self._replica = Replica(schema)
        self._now = now
        self._max_skew_ms = max_skew_ms
        self._hlc = resume[0] if resume else Hlc.initial(device_id)
        self._seq = resume[1] if resume else 0

    @property
    def replica(self) -> Replica:
        return self._replica

    @property
    def clock(self) -> Hlc:
        return self._hlc

    @property
    def seq(self) -> int:
        return self._seq

    def assign(self, record: str, field: str, value: JsonValue) -> AssignOp:
        """Writes a value; `None` is a value (JSON null), it does not clear the field."""
        deps = tuple(self._replica.observed_deps(record, field))
        op_id, hlc = self._next(record, field)
        return self._write(AssignOp(op_id, record, field, hlc, value, deps))

    def inc(self, record: str, field: str, by: int) -> IncOp:
        n = as_increment(by)
        op_id, hlc = self._next(record, field)
        return self._write(IncOp(op_id, record, field, hlc, n))

    def add(self, record: str, field: str, element: object) -> AddOp:
        e = as_set_element(element)
        deps = tuple(self._replica.observed_deps(record, field, e))
        op_id, hlc = self._next(record, field)
        return self._write(AddOp(op_id, record, field, hlc, e, deps))

    def remove(self, record: str, field: str, element: object) -> RemoveOp:
        e = as_set_element(element)
        deps = tuple(self._replica.observed_deps(record, field, e))
        op_id, hlc = self._next(record, field)
        return self._write(RemoveOp(op_id, record, field, hlc, e, deps))

    def receive(self, op: Op) -> ApplyResult:
        """Applies an op from elsewhere. Refuses it, state untouched, if its clock is absurd."""
        if self._replica.has(op.op_id):
            self.advance_seq(op.op_id)
            return "duplicate"
        self._replica.validate(op)
        nxt = self._hlc.receive(op.hlc, self._now(), self._max_skew_ms)
        result = self._replica.apply(op)
        self._hlc = nxt
        self.advance_seq(op.op_id)
        return result

    def advance_seq(self, seen_or_seq: str | int) -> None:
        """Makes sure future op ids come after `seen_or_seq`: an op id of this device (as received
        from the server after a reinstall) or a sequence number the server reports. Op ids are
        never reused."""
        if isinstance(seen_or_seq, str):
            parsed = parse_op_id(seen_or_seq)
            if parsed.device != self.device_id:
                return
            seq = parsed.seq
        elif type(seen_or_seq) is int:
            seq = seen_or_seq
        else:
            raise AccordError(f"not a sequence number: {seen_or_seq!r}")
        if seq > self._seq:
            self._seq = seq

    def discard(self, op_ids: Iterable[OpId]) -> None:
        """Rolls back ops the server refused: the replica is rebuilt from its log without them, so
        this device converges with everyone else instead of keeping a change nobody else will ever
        see. The clock and sequence number are not rewound; op ids are never reused."""
        self._replica = self._replica.without(op_ids)

    def forget(self, record: str, keep: Iterable[OpId] = ()) -> None:
        """The record left this device's scope: forget it, except the local ops in `keep`."""
        self._replica = self._replica.forget(record, keep)

    def _next(self, record: str, field: str) -> tuple[OpId, Hlc]:
        # Validate before consuming a clock tick or sequence number.
        self._replica.observed_deps(record, field)
        self._hlc = self._hlc.tick(self._now())
        self._seq += 1
        return f"{self.device_id}:{self._seq}", self._hlc

    def _write(self, op: _O) -> _O:
        self._replica.apply(op)
        return op
