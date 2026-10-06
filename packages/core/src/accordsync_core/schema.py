"""Schemas: which merge strategy each field of each record type uses."""

from __future__ import annotations

import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Literal, TypeAlias, get_args

from .errors import AccordError
from .op import OpKind, record_type

StrategyName: TypeAlias = Literal["lww", "counter", "set", "conflict"]


@dataclass(frozen=True, slots=True)
class Strategy:
    strategy: StrategyName


def lww() -> Strategy:
    """Highest clock wins. For names, notes and simple scalars."""
    return Strategy("lww")


def counter() -> Strategy:
    """Sum of all increments; none is ever lost. For quantities and stock adjustments."""
    return Strategy("counter")


def set_() -> Strategy:
    """Add-wins set of strings or numbers. For tags and assigned agents. (`set` is a builtin.)"""
    return Strategy("set")


def conflict() -> Strategy:
    """Concurrent values are all kept and the field flagged; the app resolves it. Never guesses."""
    return Strategy("conflict")


RecordFields: TypeAlias = Mapping[str, Strategy]
Schema: TypeAlias = Mapping[str, RecordFields]

KINDS: Mapping[StrategyName, tuple[OpKind, ...]] = {
    "lww": ("assign",),
    "conflict": ("assign",),
    "counter": ("inc",),
    "set": ("add", "remove"),
}

_TYPE = re.compile(r"[A-Za-z][A-Za-z0-9_]{0,63}")
_NAMES: tuple[str, ...] = get_args(StrategyName)


def define_schema(schema: Mapping[str, Mapping[str, Strategy | str]]) -> Schema:
    """Checks a schema and returns it with every strategy as a `Strategy`.

    Strategies may be named by string (`"set"`), as the golden vectors declare them.
    """
    out: dict[str, dict[str, Strategy]] = {}
    for type_, fields in schema.items():
        if not isinstance(type_, str) or not _TYPE.fullmatch(type_):
            raise AccordError(f'invalid record type "{type_}"')
        out[type_] = {}
        for field, s in fields.items():
            name = s if isinstance(s, str) else getattr(s, "strategy", None)
            if name not in _NAMES:
                raise AccordError(f"{type_}.{field}: unknown strategy")
            out[type_][field] = Strategy(name)  # type: ignore[arg-type]
    return out


def strategy_for(schema: Schema, record: str, field: str) -> StrategyName:
    """The strategy for `record.field`, or an error naming what is wrong."""
    type_ = record_type(record)
    fields = schema.get(type_)
    if fields is None:
        raise AccordError(f'unknown record type "{type_}"')
    s = fields.get(field)
    if s is None:
        raise AccordError(f'unknown field "{type_}.{field}"')
    return s.strategy


def fields_of(schema: Schema, record: str) -> RecordFields:
    type_ = record_type(record)
    fields = schema.get(type_)
    if fields is None:
        raise AccordError(f'unknown record type "{type_}"')
    return fields
