"""The golden vectors shared with `@accordsync/core`: every case, in every delivery order, each op
delivered twice, must read exactly the expected canonical snapshot."""

import itertools
import json
from pathlib import Path
from typing import Any

import pytest

from accordsync_core import Replica, canonical_json, decode_op, define_schema

VECTORS = Path(__file__).resolve().parents[3] / "contract" / "vectors"


def _cases() -> list[Any]:
    out = []
    for f in sorted(VECTORS.glob("*.json")):
        v = json.loads(f.read_text("utf-8"))
        for c in v["cases"]:
            out.append(pytest.param(v, c, id=f"{f.name}: {c['name']}"))
    return out


@pytest.mark.parametrize(("vector", "case"), _cases())
def test_golden_vector(vector: dict[str, Any], case: dict[str, Any]) -> None:
    assert vector["version"] == 1
    schema = define_schema(vector["schema"])
    ops = [decode_op(o) for o in case["ops"]]
    expected = canonical_json(case["expected"])
    orders = 0
    for order in itertools.permutations(ops):
        r = Replica(schema)
        for op in [*order, *order]:
            r.apply(op)
        assert r.snapshot() == expected, " ".join(o.op_id for o in order)
        orders += 1
    assert orders > 0
