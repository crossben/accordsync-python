from accordsync_fastapi import PROTOCOL_VERSION


def test_speaks_protocol_version_1() -> None:
    assert PROTOCOL_VERSION == 1
