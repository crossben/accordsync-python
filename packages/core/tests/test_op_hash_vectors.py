"""The op-hash golden vectors (`contract/vectors/op-hash/op-hash.json`, written by the TypeScript
server): the canonical JSON of every wire op, byte for byte. The hashes are checked in the server's
tests (`op_hash` lives there)."""

import json
from pathlib import Path
from typing import Any

import pytest

from accordsync_core import canonical_json, decode_op, encode_op

FILE = Path(__file__).resolve().parents[3] / "contract" / "vectors" / "op-hash" / "op-hash.json"
VECTORS = json.loads(FILE.read_text("utf-8"))


def test_version() -> None:
    assert VECTORS["version"] == 1
    assert len(VECTORS["cases"]) > 0


@pytest.mark.parametrize("case", VECTORS["cases"], ids=lambda c: c["name"])
def test_canonical_json_of_the_wire_op(case: dict[str, Any]) -> None:
    assert canonical_json(case["op"]) == case["canonical"]
    # Decoding and encoding changes nothing, as the server stores the op.
    assert canonical_json(encode_op(decode_op(case["op"]))) == case["canonical"]
