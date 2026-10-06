"""Port of `writer.test.ts`."""

import itertools

import pytest

from accordsync_core import (
    AccordError,
    ClockSkewError,
    FieldRef,
    LocalWriter,
    define_schema,
    lww,
    set_,
)

SCHEMA = define_schema(
    {
        "dossier": {
            "client_name": lww(),
            "documents": set_(),
            "visits": "counter",
            "status": "conflict",
        }
    }
)


def device(device_id: str, start: int = 1000) -> LocalWriter:
    clock = itertools.count(start)
    return LocalWriter(SCHEMA, device_id, now=lambda: next(clock))


def test_numbers_ops_per_device_and_stamps_increasing_clocks() -> None:
    a = device("a")
    o1 = a.assign("dossier:1", "client_name", "Awa")
    o2 = a.assign("dossier:1", "client_name", "Awa Diop")
    assert [o1.op_id, o2.op_id] == ["a:1", "a:2"]
    assert o2.hlc.compare(o1.hlc) > 0
    assert a.replica.read("dossier:1") == {"client_name": "Awa Diop", "documents": [], "visits": 0}


def test_reads_defaults_for_untouched_fields_and_leaves_never_written_ones_out() -> None:
    a = device("a")
    a.inc("dossier:1", "visits", 2)
    assert a.replica.read("dossier:1") == {"documents": [], "visits": 2}
    assert a.replica.read("dossier:404") is None


def test_a_field_assigned_none_reads_as_null() -> None:
    a = device("a")
    a.assign("dossier:1", "client_name", None)
    a.assign("dossier:1", "status", None)
    read = a.replica.read("dossier:1")
    assert read is not None
    assert read["client_name"] is None
    assert read["status"] == {"value": None}
    assert a.replica.snapshot() == (
        '{"dossier:1":{"client_name":null,"documents":[],"status":{"value":null},"visits":0}}'
    )


def test_rejects_writes_that_do_not_match_the_schema() -> None:
    a = device("a")
    with pytest.raises(AccordError, match="unknown field"):
        a.assign("dossier:1", "nope", 1)
    with pytest.raises(AccordError, match="unknown record type"):
        a.assign("ghost:1", "x", 1)
    # Nothing was consumed by writes to fields that do not exist.
    assert a.seq == 0
    with pytest.raises(AccordError, match="lww"):
        a.inc("dossier:1", "client_name", 1)
    with pytest.raises(AccordError, match="string or finite number"):
        a.add("dossier:1", "documents", {"a": 1})
    assert a.replica.read("dossier:1") is None


@pytest.mark.parametrize("by", [True, False, 1.5, "1", None, 2**53, float("nan")])
def test_refuses_counter_increments_that_are_not_safe_integers(by: object) -> None:
    a = device("a")
    with pytest.raises(AccordError):
        a.inc("dossier:1", "visits", by)  # type: ignore[arg-type]
    assert a.replica.read("dossier:1") is None


@pytest.mark.parametrize("element", [True, False, None, float("inf"), [1], {}])
def test_refuses_set_elements_that_are_not_strings_or_finite_numbers(element: object) -> None:
    a = device("a")
    with pytest.raises(AccordError):
        a.add("dossier:1", "documents", element)


def test_sets_remove_only_removes_what_the_writer_has_seen_add_wins() -> None:
    a, b = device("a"), device("b")
    add = a.add("dossier:1", "documents", "cni.pdf")
    b.receive(add)
    remove = b.remove("dossier:1", "documents", "cni.pdf")
    readd = a.add("dossier:1", "documents", "cni.pdf")  # concurrent with the remove
    a.receive(remove)
    b.receive(readd)
    for w in (a, b):
        assert w.replica.read("dossier:1") == {"documents": ["cni.pdf"], "visits": 0}


def test_sets_1_and_1_0_are_the_same_element_as_in_javascript() -> None:
    a = device("a")
    a.add("dossier:1", "documents", 1)
    a.add("dossier:1", "documents", 1.0)
    assert a.replica.snapshot() == '{"dossier:1":{"documents":[1],"visits":0}}'
    a.remove("dossier:1", "documents", 1.0)
    assert a.replica.read("dossier:1") == {"documents": [], "visits": 0}


def test_sets_the_string_1_and_the_number_1_are_different_elements() -> None:
    a = device("a")
    a.add("dossier:1", "documents", 1)
    remove = a.remove("dossier:1", "documents", "1")
    assert remove.deps == ()
    a.add("dossier:1", "documents", "1")
    assert a.replica.snapshot() == '{"dossier:1":{"documents":[1,"1"],"visits":0}}'


def test_sets_true_is_never_confused_with_1() -> None:
    a = device("a")
    a.add("dossier:1", "documents", 1)
    with pytest.raises(AccordError):
        a.remove("dossier:1", "documents", True)
    assert a.replica.read("dossier:1") == {"documents": [1], "visits": 0}


def test_sets_numbers_first_then_strings_by_utf16_code_unit() -> None:
    a = device("a")
    for e in ["", "😀", "b", 10, -1.5, "B", 2]:
        a.add("dossier:1", "documents", e)
    read = a.replica.read("dossier:1")
    assert read is not None
    # U+1F600 is a surrogate pair (0xD83D...), which sorts before U+E000 by code unit.
    assert read["documents"] == [-1.5, 2, 10, "B", "b", "😀", ""]


def test_conflict_concurrent_assigns_surface_both_values_nothing_is_guessed() -> None:
    a, b = device("a"), device("b")
    x = a.assign("dossier:1", "status", "approved")
    y = b.assign("dossier:1", "status", "rejected")
    a.receive(y)
    b.receive(x)
    for r in (a.replica, b.replica):
        read = r.read("dossier:1")
        assert read is not None
        assert read["status"] == {
            "conflicted": [
                {"value": "approved", "opId": "a:1"},
                {"value": "rejected", "opId": "b:1"},
            ]
        }
        assert r.conflicts() == [FieldRef("dossier:1", "status")]


def test_conflict_a_sequential_edit_replaces_the_value_it_saw_without_a_conflict() -> None:
    a, b = device("a"), device("b")
    b.receive(a.assign("dossier:1", "status", "draft"))
    a.receive(b.assign("dossier:1", "status", "submitted"))
    read = a.replica.read("dossier:1")
    assert read is not None
    assert read["status"] == {"value": "submitted"}
    assert a.replica.conflicts() == []


def test_conflict_resolving_keeps_an_edit_the_resolver_had_not_seen() -> None:
    a, b, c = device("a"), device("b"), device("c")
    x = a.assign("dossier:1", "status", "approved")
    y = b.assign("dossier:1", "status", "rejected")
    z = c.assign("dossier:1", "status", "on_hold")  # c is offline the whole time
    a.receive(y)
    resolution = a.assign("dossier:1", "status", "approved")  # resolves x and y
    assert resolution.deps == ("a:1", "b:1")
    for op in (resolution, z, x):
        b.receive(op)
    read = b.replica.read("dossier:1")
    assert read is not None
    assert read["status"] == {
        "conflicted": [
            {"value": "approved", "opId": "a:2"},
            {"value": "on_hold", "opId": "c:1"},
        ]
    }


def test_counts_every_increment_including_negative_ones() -> None:
    a, b = device("a"), device("b")
    ops = [a.inc("dossier:1", "visits", 3), b.inc("dossier:1", "visits", -1)]
    a.receive(ops[1])
    b.receive(ops[0])
    for w in (a, b):
        read = w.replica.read("dossier:1")
        assert read is not None
        assert read["visits"] == 2


def test_ignores_a_duplicate_op() -> None:
    a, b = device("a"), device("b")
    op = a.inc("dossier:1", "visits", 5)
    assert b.receive(op) == "applied"
    assert b.receive(op) == "duplicate"
    read = b.replica.read("dossier:1")
    assert read is not None
    assert read["visits"] == 5


def test_lww_highest_clock_wins_regardless_of_arrival_order() -> None:
    a, b = device("a", 1000), device("b", 5000)  # b's clock is ahead
    x = b.assign("dossier:1", "client_name", "from b")
    y = a.assign("dossier:1", "client_name", "from a")
    a.receive(x)
    b.receive(y)
    for w in (a, b):
        read = w.replica.read("dossier:1")
        assert read is not None
        assert read["client_name"] == "from b"


def test_refuses_an_op_from_a_clock_too_far_ahead_and_leaves_state_untouched() -> None:
    a = LocalWriter(SCHEMA, "a", now=lambda: 1000, max_skew_ms=60000)
    liar = LocalWriter(SCHEMA, "liar", now=lambda: 10000000)
    clock = a.clock
    with pytest.raises(ClockSkewError):
        a.receive(liar.inc("dossier:1", "visits", 1))
    assert a.replica.read("dossier:1") is None
    assert a.clock == clock


def test_discard_rolls_back_a_refused_op_and_keeps_the_rest() -> None:
    a = device("a")
    keep = a.inc("dossier:1", "visits", 2)
    refused = a.inc("dossier:1", "visits", 40)
    a.assign("dossier:2", "status", "approved")
    a.discard([refused.op_id])
    assert a.replica.read("dossier:1") == {"documents": [], "visits": 2}
    assert a.replica.has(keep.op_id)
    assert not a.replica.has(refused.op_id)
    read = a.replica.read("dossier:2")
    assert read is not None
    assert read["status"] == {"value": "approved"}
    assert a.inc("dossier:1", "visits", 1).op_id == "a:4"  # op ids are never reused


def test_never_reuses_an_op_id_after_receiving_its_own_old_ops() -> None:
    before = device("a")
    old = [before.inc("dossier:1", "visits", 1), before.inc("dossier:1", "visits", 2)]
    reinstalled = device("a")  # fresh storage, same id
    for op in old:
        reinstalled.receive(op)
    assert reinstalled.inc("dossier:1", "visits", 4).op_id == "a:3"
    read = reinstalled.replica.read("dossier:1")
    assert read is not None
    assert read["visits"] == 7


def test_advance_seq_from_a_number_or_this_devices_op_id_only() -> None:
    a = device("a")
    a.advance_seq(5)
    a.advance_seq("b:99")  # another device's id says nothing about ours
    a.advance_seq(3)  # never goes back
    assert a.inc("dossier:1", "visits", 1).op_id == "a:6"
    a.advance_seq("a:10")
    assert a.inc("dossier:1", "visits", 1).op_id == "a:11"
    with pytest.raises(AccordError):
        a.advance_seq(True)


def test_resume_continues_the_clock_and_sequence() -> None:
    a = device("a")
    a.inc("dossier:1", "visits", 1)
    b = LocalWriter(SCHEMA, "a", now=lambda: 0, resume=(a.clock, a.seq))
    op = b.inc("dossier:1", "visits", 1)
    assert op.op_id == "a:2"
    assert op.hlc.compare(a.clock) > 0


def test_sets_re_adding_a_present_element_replaces_the_tags_its_writer_saw() -> None:
    a, b = device("a"), device("b")
    for _ in range(50):
        a.add("dossier:1", "documents", "cni.pdf")
    assert len(a.replica.observed_deps("dossier:1", "documents", "cni.pdf")) == 1
    for op in a.replica.ops():
        b.receive(op)
    remove = b.remove("dossier:1", "documents", "cni.pdf")
    readd = a.add("dossier:1", "documents", "cni.pdf")
    a.receive(remove)
    b.receive(readd)
    for w in (a, b):
        read = w.replica.read("dossier:1")
        assert read is not None
        assert read["documents"] == ["cni.pdf"]


def test_forget_drops_a_record_except_the_kept_local_ops() -> None:
    a = device("a")
    mine = a.inc("dossier:1", "visits", 1)
    a.inc("dossier:1", "visits", 2)
    a.inc("dossier:2", "visits", 3)
    a.forget("dossier:1", {mine.op_id})
    assert a.replica.read("dossier:1") == {"documents": [], "visits": 1}
    assert a.replica.read("dossier:2") == {"documents": [], "visits": 3}
    a.forget("dossier:2")
    assert a.replica.read("dossier:2") is None
