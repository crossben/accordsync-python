"""The shared contract (golden vectors, protocol schemas), committed in `contract/`.

Y1 makes the core pass every vector; for now, check the contract is there and readable.
"""

import json
from pathlib import Path

from accordsync_core import PROTOCOL_VERSION

CONTRACT = Path(__file__).resolve().parents[3] / "contract"


def test_speaks_protocol_version_1() -> None:
    assert PROTOCOL_VERSION == 1


def test_golden_vectors_are_present() -> None:
    files = sorted((CONTRACT / "vectors").glob("*.json"))
    assert [f.name for f in files] == ["conflict.json", "counter.json", "lww.json", "set.json"]
    for f in files:
        assert json.loads(f.read_text("utf-8"))["version"] == 1, f.name
    assert (CONTRACT / "vectors" / "random" / "cases.json").is_file()


def test_protocol_schemas_are_present() -> None:
    for name in ["WireOp", "PushRequest", "PushResponse", "PullItem", "PullResponse"]:
        assert (CONTRACT / "protocol" / "v1" / f"{name}.schema.json").is_file(), name
