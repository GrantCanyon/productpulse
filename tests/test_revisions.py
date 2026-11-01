"""Независимые денежные примеры объяснения ревизии исторических срезов."""

import json

import pytest
from conftest import event, journey

from productpulse.analytics import compare

BEFORE = "2026-01-07T00:00:00Z"
AFTER = "2026-01-09T00:00:00Z"
EARLY = "2026-01-06T00:00:00Z"
LATE = "2026-01-08T00:00:00Z"
MONEY = ("gross_cents", "reversed_cents", "net_cents")


def load(store, events, received=EARLY):
    store.ingest([json.dumps(e) for e in events], source="ревизия", received_at=received)


def diff(store, **kwargs):
    return compare(
        store, start="2026-01-01", end="2026-02-01", before=BEFORE, after=AFTER, **kwargs
    )


def contribution(gross=0, reversed_cents=0, net=0):
    return dict(zip(MONEY, (gross, reversed_cents, net), strict=True))


def verify(store, result):
    revision = result["revision"]
    changes = revision["ledger_changes"]
    assert revision["ledger_delta"] == {key: result["delta"][key] for key in MONEY}
    assert revision["ledger_delta"] == {
        key: sum(change["contribution"][key] for change in changes) for key in MONEY
    }
    assert revision["ledger_delta"]["net_cents"] == (
        revision["ledger_delta"]["gross_cents"] - revision["ledger_delta"]["reversed_cents"]
    )
    accepted = {row["id"]: row for row in store.read() if row["status"] == "accepted"}
    rebuilt = {entry["event_id"]: entry for entry in result["before"]["ledger"]}
    for change in changes:
        assert change["before"] is not None or change["after"] is not None
        for side in ("before", "after"):
            entry = change[side]
            if not entry:
                continue
            assert [ref["event_id"] for ref in entry["source_refs"]] == entry["source_event_ids"]
            cutoff = result[side]["window"]["as_of"]
            for ref in entry["source_refs"]:
                assert accepted[ref["receipt_id"]]["event_id"] == ref["event_id"]
                assert ref["received_at"] <= cutoff
        if change["after"] is None:
            rebuilt.pop(change["event_id"])
        else:
            rebuilt[change["event_id"]] = {
                k: v for k, v in change["after"].items() if k != "source_refs"
            }
    assert rebuilt == {entry["event_id"]: entry for entry in result["after"]["ledger"]}
    return {change["event_id"]: change for change in changes}


def test_new_payment_has_exact_contribution_and_accepted_source_refs(store):
    load(store, journey(), LATE)
    result = diff(store)
    row = verify(store, result)["s1-paid"]
    assert row["change"] == "added"
    assert row["reason"] == "new_delivery"
    assert row["operation_type"] == "payment"
    assert row["contribution"] == contribution(10000, 0, 10000)
    assert row["evidence"]["before_known"] is False
    assert len(row["after"]["source_refs"]) == 4


def test_known_payment_becomes_valid_when_predecessors_arrive(store):
    entries = journey()
    load(store, [entries[-1]])
    load(store, entries[:-1], LATE)
    result = diff(store)
    row = verify(store, result)["s1-paid"]
    assert row["reason"] == "chain_became_valid"
    assert row["evidence"]["before_known"] is True
    assert row["evidence"]["before_exclusion"] == "missing_predecessor"
    assert set(row["evidence"]["newly_known_source_event_ids"]) == {
        e["event_id"] for e in entries[:-1]
    }


def test_new_cancellation_reverses_payment_before_report_window(store):
    load(store, journey(day="2025-12-30"))
    load(store, [event("cancelled", "2026-01-05T11:00:00Z")], LATE)
    result = diff(store)
    rows = verify(store, result)
    assert set(rows) == {"s1-cancelled"}
    row = rows["s1-cancelled"]
    assert row["reason"] == "new_delivery"
    assert row["operation_type"] == "cancellation"
    assert row["contribution"] == contribution(0, 10000, -10000)
    assert any(ref["occurred_at"].startswith("2025-12-30") for ref in row["after"]["source_refs"])


def test_known_cancellation_and_new_payment_restore_to_zero_net(store):
    load(store, [event("cancelled", "2026-01-05T11:00:00Z")])
    load(store, journey(), LATE)
    result = diff(store)
    rows = verify(store, result)
    assert rows["s1-cancelled"]["reason"] == "chain_became_valid"
    assert rows["s1-paid"]["reason"] == "new_delivery"
    assert result["revision"]["ledger_delta"] == contribution(10000, 10000, 0)


def test_earlier_payment_revalues_cancellation_without_net_change(store):
    load(store, journey() + [event("cancelled", "2026-01-05T11:00:00Z")])
    load(store, [event("paid", "2026-01-05T10:02:30Z", eid="p0", cents=7000)], LATE)
    result = diff(store)
    rows = verify(store, result)
    assert len(rows) == 3
    assert rows["s1-paid"]["change"] == "removed"
    assert rows["s1-paid"]["evidence"]["after_exclusion"] == "duplicate_payment"
    assert rows["s1-paid"]["contribution"] == contribution(-10000, 0, -10000)
    assert rows["p0"]["contribution"] == contribution(7000, 0, 7000)
    cancelled = rows["s1-cancelled"]
    assert cancelled["change"] == "changed"
    assert cancelled["reason"] == "payment_relinked"
    assert cancelled["before"]["payment_event_id"] == "s1-paid"
    assert cancelled["after"]["payment_event_id"] == "p0"
    assert cancelled["contribution"] == contribution(0, -3000, 3000)
    assert result["revision"]["ledger_delta"] == contribution(-3000, -3000, 0)


def test_source_only_change_is_not_called_payment_relink(store):
    load(store, journey())
    load(store, [event("viewed", "2026-01-05T09:59:00Z", eid="earlier-view")], LATE)
    result = diff(store)
    row = verify(store, result)["s1-paid"]
    assert row["change"] == "changed"
    assert row["reason"] == "provenance_changed"
    assert row["contribution"] == contribution()
    assert row["before"]["source_event_ids"][0] == "s1-viewed"
    assert row["after"]["source_event_ids"][0] == "earlier-view"


def test_funnel_only_revision_is_separate_from_money(store):
    load(store, [event("viewed")])
    load(store, [event("quoted", "2026-01-05T10:01:00Z")], LATE)
    result = diff(store)
    assert verify(store, result) == {}
    assert result["revision"]["ledger_delta"] == contribution()
    rows = result["revision"]["funnel_changes"]
    assert len(rows) == 1
    assert rows[0]["stage"] == "quoted"
    assert rows[0]["before_sessions"] == 0
    assert rows[0]["after_sessions"] == 1
    assert rows[0]["delta_sessions"] == 1
    assert rows[0]["added_session_ids"] == ["s1"]
    assert {ref["event_id"] for ref in rows[0]["after_source_refs"]} == {"s1-viewed", "s1-quoted"}


def test_unrelated_segment_has_no_revision_money_or_funnel(store):
    load(store, journey())
    load(
        store,
        [dict(e, channel="search") for e in journey(user="u2", session="s2", rental="r2")],
        LATE,
    )
    result = diff(store, channel="organic")
    assert verify(store, result) == {}
    assert result["revision"]["funnel_changes"] == []


def test_duplicate_and_conflict_do_not_change_canonical_revision(store):
    entries = journey()
    load(store, entries)
    load(store, entries + [dict(entries[-1], amount_cents=50000)], LATE)
    result = diff(store)
    assert verify(store, result) == {}
    assert result["revision"]["funnel_changes"] == []
    assert result["before"]["quality"] != result["after"]["quality"]


def test_compare_uses_one_captured_history_even_if_backdated_ingest_follows(store, monkeypatch):
    original_read = store.read
    inserted = False

    def capture_then_ingest(**kwargs):
        nonlocal inserted
        rows = original_read(**kwargs)
        if not inserted:
            inserted = True
            load(store, journey(), EARLY)
        return rows

    monkeypatch.setattr(store, "read", capture_then_ingest)
    result = diff(store)
    assert result["delta"]["net_cents"] == 0
    assert result["before"]["quality"]["accepted"] == 0
    assert result["after"]["quality"]["accepted"] == 0
    assert result["revision"]["ledger_changes"] == []
    assert original_read()


def test_equal_cutoffs_and_inclusive_microsecond_boundary(store):
    load(store, journey(), "2026-01-07T00:00:00.000001Z")
    result = compare(
        store,
        start="2026-01-01",
        end="2026-02-01",
        before=BEFORE,
        after="2026-01-07T00:00:00.000001Z",
    )
    assert verify(store, result)["s1-paid"]["contribution"] == contribution(10000, 0, 10000)
    equal = compare(store, start="2026-01-01", end="2026-02-01", before=AFTER, after=AFTER)
    assert verify(store, equal) == {}
    assert equal["revision"]["funnel_changes"] == []


def test_reverse_cutoffs_are_rejected(store):
    with pytest.raises(ValueError, match="after"):
        compare(store, start="2026-01-01", end="2026-02-01", before=AFTER, after=BEFORE)
