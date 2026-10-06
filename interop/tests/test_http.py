"""The Python client over real HTTP against the real server: the scenarios of the TypeScript e2e
suite whose outcome depends on the server (refusal reasons, exits, scope deltas, compaction,
device_seq). Port of flutter/interop/test/http_test.dart."""

from __future__ import annotations

from collections.abc import Callable, Iterator
from pathlib import Path

import pytest
from accordsync import (
    AccordClient,
    ConflictInfo,
    ConflictValue,
    HttpError,
    HttpTransport,
    MemoryStorage,
    Refusal,
    SqliteStorage,
    StorageAdapter,
)
from support import (
    SERVER_URL,
    compact_server,
    require_server,
    reset_server,
    schema,
    snapshot_of,
    token_for,
)

Open = Callable[..., AccordClient]


@pytest.fixture(scope="module", autouse=True)
def _server() -> None:
    require_server()


@pytest.fixture
def tokens() -> dict[str, str]:
    reset_server()
    return {
        "awa": token_for("awa", ["dakar"]),
        "moussa": token_for("moussa", ["dakar"]),
        "fatou": token_for("fatou", ["thies"]),
    }


@pytest.fixture
def open_(tokens: dict[str, str]) -> Iterator[Open]:
    opened: list[AccordClient] = []

    def open_client(
        user: str, device_id: str, storage: StorageAdapter | None = None
    ) -> AccordClient:
        c = AccordClient.open(
            schema=schema,
            storage=storage if storage is not None else MemoryStorage(),
            device_id=device_id,
            transport=HttpTransport(SERVER_URL, lambda: tokens[user]),
        )
        opened.append(c)
        return c

    yield open_client
    for c in opened:
        c.close()


def sync_all(cs: list[AccordClient]) -> None:
    for _ in range(2):
        for c in cs:
            c.sync()


def test_two_agents_edit_offline_then_converge(open_: Open) -> None:
    awa = open_("awa", "awa-phone")
    moussa = open_("moussa", "moussa-phone")
    awa.assign("dossier:1", "zone", "dakar")
    awa.assign("dossier:1", "agent", "awa")
    sync_all([awa, moussa])
    awa.inc("dossier:1", "visits", 2)
    awa.add("dossier:1", "docs", "cni.pdf")
    moussa.inc("dossier:1", "visits", 3)
    moussa.assign("dossier:1", "client_name", "Aminata Fall")
    moussa.assign("dossier:1", "status", "rejected")
    awa.assign("dossier:1", "status", "approved")
    sync_all([awa, moussa])
    for c in (awa, moussa):
        assert c.read("dossier:1") == {
            "agent": "awa",
            "zone": "dakar",
            "client_name": "Aminata Fall",
            "status": {
                "conflicted": [
                    {"value": "approved", "opId": "awa-phone:5"},
                    {"value": "rejected", "opId": "moussa-phone:3"},
                ]
            },
            "visits": 5,
            "docs": ["cni.pdf"],
        }
        assert c.conflicts() == [
            ConflictInfo(
                "dossier:1",
                "status",
                (
                    ConflictValue("approved", "awa-phone:5"),
                    ConflictValue("rejected", "moussa-phone:3"),
                ),
            )
        ]
        assert c.status().pending == 0
    moussa.resolve("dossier:1", "status", "approved")
    sync_all([awa, moussa])
    assert awa.conflicts() == []
    assert snapshot_of(awa) == snapshot_of(moussa)


def test_refused_write_is_rolled_back_with_the_server_reason(open_: Open) -> None:
    fatou = open_("fatou", "fatou-phone")
    awa = open_("awa", "awa-phone")
    fatou.assign("dossier:7", "zone", "thies")
    fatou.sync()
    refusals: list[Refusal] = []
    awa.on("refused", refusals.append)
    awa.assign("dossier:7", "client_name", "not mine")
    awa.sync()
    assert refusals == [
        Refusal(
            "awa-phone:1", "dossier:7", "client_name", "out of scope: you may not write dossier:7"
        )
    ]
    assert awa.read("dossier:7") is None


def test_record_reassigned_away_leaves_the_device_and_reaches_the_new_agent(open_: Open) -> None:
    awa = open_("awa", "awa-phone")
    fatou = open_("fatou", "fatou-phone")
    awa.assign("dossier:3", "agent", "awa")
    awa.inc("dossier:3", "visits", 4)
    awa.sync()
    awa.assign("dossier:3", "agent", "fatou")
    awa.sync()
    assert awa.read("dossier:3") is None
    fatou.sync()
    assert fatou.read("dossier:3") == {"agent": "fatou", "visits": 4, "docs": []}


def test_follows_a_change_of_read_scopes_as_a_delta(open_: Open, tokens: dict[str, str]) -> None:
    fatou = open_("fatou", "fatou-phone")
    fatou.assign("dossier:8", "zone", "thies")
    fatou.sync()
    awa = open_("awa", "awa-phone")
    awa.assign("dossier:1", "zone", "dakar")
    awa.sync()
    tokens["awa"] = token_for("awa", ["dakar", "thies"])
    awa.inc("dossier:8", "visits", 1)
    resyncs: list[object] = []
    awa.on("resync", resyncs.append)
    awa.sync()
    assert resyncs == []
    assert awa.records() == ["dossier:1", "dossier:8"]
    assert awa.read("dossier:8") == {"zone": "thies", "visits": 1, "docs": []}


def test_after_compaction_a_new_device_gets_the_snapshot(open_: Open, tmp_path: Path) -> None:
    awa = open_("awa", "awa-phone")
    awa.assign("dossier:1", "zone", "dakar")
    for _ in range(4):
        awa.inc("dossier:1", "visits", 1)
    awa.add("dossier:1", "docs", "a.pdf")
    awa.remove("dossier:1", "docs", "a.pdf")
    awa.assign("dossier:1", "status", "submitted")
    sync_all([awa])
    assert compact_server()["records"] == 1

    path = tmp_path / "tablet.db"
    storage = SqliteStorage(path)
    tablet = open_("awa", "awa-tablet", storage)
    tablet.inc("dossier:1", "visits", 10)
    tablet.sync()
    before = awa.read("dossier:1")
    assert before is not None
    assert tablet.read("dossier:1") == {**before, "visits": 14}
    assert [s.record for s in storage.load().snapshots] == ["dossier:1"]
    awa.sync()
    assert snapshot_of(awa) == snapshot_of(tablet)

    # Restart from the same file: the snapshot, the ops on top and the cursor are all restored.
    expected, cursor = snapshot_of(tablet), tablet.status().cursor
    tablet.close()
    storage.close()
    again_storage = SqliteStorage(path)
    again = open_("awa", "ignored-id", again_storage)
    assert again.device_id == "awa-tablet"
    assert snapshot_of(again) == expected
    assert again.status().cursor == cursor
    assert again.status().pending == 0
    again.sync()
    assert snapshot_of(again) == expected
    again.close()
    again_storage.close()


def test_device_seq_reinstalled_device_never_reuses_an_op_id(open_: Open) -> None:
    before = open_("awa", "awa-tablet")
    before.assign("dossier:1", "zone", "dakar")
    before.inc("dossier:1", "visits", 2)
    before.sync()
    before.sync()
    compact_server()
    after = open_("awa", "awa-tablet")
    after.sync()
    assert after.inc("dossier:1", "visits", 3).op_id == "awa-tablet:3"
    after.sync()
    assert after.status().pending == 0
    record = after.read("dossier:1")
    assert record is not None
    assert record["visits"] == 5


def test_reinstalled_device_writing_before_first_sync_gets_op_id_already_used(
    open_: Open,
) -> None:
    before = open_("awa", "awa-old")
    before.assign("dossier:1", "zone", "dakar")
    before.sync()
    after = open_("awa", "awa-old")
    refusals: list[Refusal] = []
    after.on("refused", refusals.append)
    after.inc("dossier:1", "visits", 1)
    after.sync()
    assert len(refusals) == 1
    assert "op id already used" in refusals[0].reason


def test_http_errors_carry_the_status(tokens: dict[str, str]) -> None:
    c = AccordClient.open(
        schema=schema,
        storage=MemoryStorage(),
        device_id="nobody",
        transport=HttpTransport(SERVER_URL, lambda: "not-a-jwt"),
    )
    with pytest.raises(HttpError) as e:
        c.sync()
    assert e.value.status == 401
    c.close()
