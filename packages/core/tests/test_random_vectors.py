"""Random scenarios written by the TypeScript core (`random-vectors.test.ts`), with the snapshots it
read. This core must produce the same bytes: same merge, same canonical JSON (key order, number and
string formatting), in any delivery order, and the same compacted record snapshots."""

import json
import random
from pathlib import Path
from typing import Any

import pytest

from accordsync_core import Replica, canonical_json, decode_op, define_schema, encode_op
from accordsync_core._text import utf16_key

FILE = Path(__file__).resolve().parents[3] / "contract" / "vectors" / "random" / "cases.json"
# json.loads keeps lone surrogates ("\ud800") as they are: never re-encode this with plain utf-8.
V = json.loads(FILE.read_text("utf-8"))
SCHEMA = define_schema(V["schema"])


def test_there_are_cases_to_check() -> None:
    assert len(V["cases"]) >= 40


@pytest.mark.parametrize("case", V["cases"], ids=lambda c: f"seed {c['seed']}")
def test_random_vector(case: dict[str, Any]) -> None:
    ops = [decode_op(o) for o in case["ops"]]
    rnd = random.Random(case["seed"])
    for k in range(6):
        order = list(ops)
        if k > 0:
            order += ops[: rnd.randrange(5)]
            rnd.shuffle(order)
        r = Replica(SCHEMA)
        for op in order:
            r.apply(op)
        assert r.snapshot() == case["snapshot"], f"order {k}"
        if k == 0:
            records: dict[str, str] = case["records"]
            assert r.records() == sorted(records, key=utf16_key)
            for record, expected in records.items():
                assert canonical_json(r.snapshot_record(record).to_json()) == expected, record
    # Wire round trip: re-encoding gives the ops back unchanged.
    assert canonical_json([encode_op(o) for o in ops]) == canonical_json(case["ops"])
