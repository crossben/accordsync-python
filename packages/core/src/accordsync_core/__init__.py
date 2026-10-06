"""Accord's merge core: hybrid logical clocks, operations and the four merge strategies."""

from .canonical import canonical_json
from .errors import AccordError, ClockSkewError
from .hlc import MAX_COUNTER, MAX_SAFE_INTEGER, Hlc, assert_node, compare_hlc
from .op import (
    AddOp,
    AssignOp,
    IncOp,
    JsonValue,
    Op,
    OpId,
    OpKind,
    ParsedOpId,
    RemoveOp,
    SetElement,
    compare_op_ids,
    parse_op_id,
    record_type,
)
from .replica import ApplyResult, FieldRef, RecordSnapshot, Replica
from .schema import (
    KINDS,
    RecordFields,
    Schema,
    Strategy,
    StrategyName,
    conflict,
    counter,
    define_schema,
    fields_of,
    lww,
    set_,
    strategy_for,
)
from .strategies import ABSENT, FieldSnapshot
from .wire import WireOp, decode_op, encode_op
from .writer import DEFAULT_MAX_SKEW_MS, LocalWriter

PROTOCOL_VERSION = 1
"""The Accord sync protocol this package speaks (docs/protocol.md in the Accord repository).
A breaking change to the wire format bumps it."""

__all__ = [
    "ABSENT",
    "DEFAULT_MAX_SKEW_MS",
    "KINDS",
    "MAX_COUNTER",
    "MAX_SAFE_INTEGER",
    "PROTOCOL_VERSION",
    "AccordError",
    "AddOp",
    "ApplyResult",
    "AssignOp",
    "ClockSkewError",
    "FieldRef",
    "FieldSnapshot",
    "Hlc",
    "IncOp",
    "JsonValue",
    "LocalWriter",
    "Op",
    "OpId",
    "OpKind",
    "ParsedOpId",
    "RecordFields",
    "RecordSnapshot",
    "RemoveOp",
    "Replica",
    "Schema",
    "SetElement",
    "Strategy",
    "StrategyName",
    "WireOp",
    "assert_node",
    "canonical_json",
    "compare_hlc",
    "compare_op_ids",
    "conflict",
    "counter",
    "decode_op",
    "define_schema",
    "encode_op",
    "fields_of",
    "lww",
    "parse_op_id",
    "record_type",
    "set_",
    "strategy_for",
]
