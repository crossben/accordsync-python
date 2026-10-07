"""op_hash must equal the TypeScript server's opHash: both store it in compacted_ops."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from accordsync_core import decode_op, encode_op
from accordsync_server import op_hash

from .conftest import run_ts

# Wire ops as JSON text (so lone surrogates survive), with the hashes the TypeScript server
# (`opHash` in app/packages/server/src/sync.ts) computes for them.
OPS_JSON = (Path(__file__).parent / "ts" / "hash_ops.json").read_text(encoding="utf-8")
TS_HASHES = [
    "6b64bfdefd08887e900a9b726f3ca42614d8899018be948a9e34f1b11141a5c4",
    "47b95ee30698c798f3cb7a921db3587f873b5a3bcf3d8bfde11046d048b2d746",
    "4d8c47e64284586a896ca5242ea7b606e424a53f65f45bba62ce1df9759c488c",
    "c4444a215cb358e61bddbbe2754a0a84cdea77ea0f55430d99004f637378d1d8",
]


def test_op_hash_equals_the_typescript_value() -> None:
    ops = json.loads(OPS_JSON)
    assert "\ud800" in ops[0]["value"]  # a lone surrogate, as JSON may carry
    assert [op_hash(op) for op in ops] == TS_HASHES


def test_op_hash_of_a_decoded_and_encoded_op_is_unchanged() -> None:
    # The server hashes encode_op(decode_op(wire)), as the TypeScript server does.
    ops = json.loads(OPS_JSON)
    assert [op_hash(encode_op(decode_op(op))) for op in ops] == TS_HASHES


def test_op_hash_is_lowercase_sha256_hex() -> None:
    h = op_hash(json.loads(OPS_JSON)[1])
    assert len(h) == 64
    assert h == h.lower()


def test_op_hash_matches_the_live_typescript_server() -> None:
    """With ACCORD_APP_DIR: recomputes the vectors with the TypeScript code itself."""
    assert json.loads(run_ts("hash", stdin=OPS_JSON)) == TS_HASHES


_VECTORS = json.loads(
    (
        Path(__file__).resolve().parents[3] / "contract" / "vectors" / "op-hash" / "op-hash.json"
    ).read_text("utf-8")
)


@pytest.mark.parametrize("case", _VECTORS["cases"], ids=lambda c: c["name"])
def test_op_hash_golden_vector(case: dict[str, Any]) -> None:
    assert op_hash(case["op"]) == case["hash"]
    assert op_hash(encode_op(decode_op(case["op"]))) == case["hash"]
