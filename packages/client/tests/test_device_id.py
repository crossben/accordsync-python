import re

from accordsync import random_device_id
from accordsync_core import assert_node


def test_device_ids_are_32_random_hex_digits_behind_a_letter() -> None:
    ids = {random_device_id() for _ in range(100)}
    assert len(ids) == 100
    for i in ids:
        assert re.fullmatch(r"d[0-9a-f]{32}", i)
        assert_node(i)


def test_device_ids_come_from_secrets(monkeypatch: object) -> None:
    import secrets

    import pytest

    mp = pytest.MonkeyPatch()
    try:
        mp.setattr(secrets, "token_hex", lambda n: "ab" * n)
        assert random_device_id() == "d" + "ab" * 16
    finally:
        mp.undo()


def test_client_generates_a_device_id_when_none_is_given() -> None:
    from accordsync import AccordClient, MemoryStorage, define_schema, lww

    class NoNetwork:
        def push(self, *_: object) -> object:  # pragma: no cover
            raise AssertionError

        def pull(self, *_: object) -> object:  # pragma: no cover
            raise AssertionError

    storage = MemoryStorage()
    c = AccordClient.open(
        schema=define_schema({"t": {"f": lww()}}),
        storage=storage,
        transport=NoNetwork(),  # type: ignore[arg-type]
    )
    assert re.fullmatch(r"d[0-9a-f]{32}", c.device_id)
    meta = storage.load().meta
    assert meta is not None
    assert meta.device_id == c.device_id
