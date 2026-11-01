import json

import pytest
from conftest import event, journey

from productpulse.analytics import report

RECEIVED = "2026-01-10T00:00:00Z"


def load(store, events, received=RECEIVED):
    return store.ingest([json.dumps(e) for e in events], source="fixture", received_at=received)


def snapshot(store, **kwargs):
    return report(store, start="2026-01-01", end="2026-02-01", **kwargs)


def test_replay_does_not_double_revenue_and_conflict_preserves_original(store):
    events = journey()
    assert load(store, events)["accepted"] == 4
    assert load(store, events)["duplicate"] == 4
    changed = dict(events[-1], amount_cents=999999)
    assert load(store, [changed])["conflict"] == 1
    result = snapshot(store)
    assert result["metrics"]["gross_cents"] == 10000
    assert result["quality"] == {"accepted": 4, "duplicate": 4, "conflict": 1, "invalid": 0}
    assert result["ledger"][0]["event_id"] == "s1-paid"
    assert result["ledger"][0]["amount_cents"] == 10000


def test_asof_reconstructs_late_predecessor_without_future_leak(store):
    events = journey()
    load(store, [events[-1]], received="2026-01-06T00:00:00Z")
    load(store, events[:-1], received="2026-01-08T00:00:00Z")
    early = snapshot(store, as_of="2026-01-07T00:00:00Z")
    late = snapshot(store, as_of="2026-01-09T00:00:00Z")
    assert early["metrics"]["gross_cents"] == 0
    assert early["exclusions"][0]["reason"] == "missing_predecessor"
    assert late["metrics"]["gross_cents"] == 10000
    assert early["quality"]["accepted"] == 1
    assert late["quality"]["accepted"] == 4
    assert early["fingerprint"] != late["fingerprint"]


def test_cancellation_reverses_in_its_own_window_and_only_once(store):
    load(store, journey(day="2025-12-30"))
    load(
        store,
        [
            event("cancelled", "2026-01-05T12:00:00Z"),
            event("cancelled", "2026-01-06T12:00:00Z", eid="second-cancel"),
        ],
    )
    result = snapshot(store)
    assert result["metrics"]["gross_cents"] == 0
    assert result["metrics"]["reversed_cents"] == 10000
    assert result["metrics"]["net_cents"] == -10000
    assert result["ledger"][0]["payment_event_id"] == "s1-paid"
    assert any(e["reason"] == "duplicate_cancellation" for e in result["exclusions"])


def test_funnel_is_ordered_distinct_sessions_not_bag_of_events(store):
    load(store, journey())
    load(
        store,
        [
            event("viewed", session="s2", rental="r2"),
            event("paid", "2026-01-05T11:00:00Z", session="s2", rental="r2", cents=9000),
        ],
    )
    result = snapshot(store)
    assert [s["sessions"] for s in result["funnel"]] == [2, 1, 1, 1]
    assert result["metrics"]["conversion_pct"] == 50.0
    assert result["metrics"]["gross_cents"] == 10000


def test_event_window_half_open_and_timezone_normalized(store):
    load(store, journey(day="2026-01-31"))
    load(store, [event("cancelled", "2026-02-01T01:00:00+01:00")])
    result = snapshot(store)
    assert result["metrics"]["reversed_cents"] == 0
    assert result["metrics"]["gross_cents"] == 10000


def test_first_payment_cohort_and_unobserved_weeks(store):
    load(store, journey())
    load(store, journey(day="2026-01-13", session="s2", rental="r2"))
    result = report(store, start="2026-01-01", end="2026-01-15")
    cohort = result["cohorts"][0]
    assert cohort["week"] == "2026-01-05"
    assert cohort["users"] == 1
    assert cohort["retention"][:3] == [100.0, 100.0, None]


@pytest.mark.parametrize(
    "bad",
    [
        "not-json",
        "[]",
        '{"event_type":"paid"}',
        json.dumps(event("paid", cents=-1)),
        json.dumps(event("paid", cents=True)),
        json.dumps(event("viewed", at="2026-01-01T10:00:00")),
        json.dumps(event("unknown")),
    ],
)
def test_bad_rows_quarantined_without_aborting_good_neighbors(store, bad):
    result = store.ingest([bad, json.dumps(event("viewed"))], source="mixed", received_at=RECEIVED)
    assert result == {"accepted": 1, "duplicate": 0, "conflict": 0, "invalid": 1}
    assert snapshot(store)["quality"]["invalid"] == 1
    rows = store.receipts(status="invalid")
    assert rows["total"] == 1
    assert rows["items"][0]["raw_json"] == bad
    assert rows["items"][0]["reason"]


def test_fingerprint_stable_after_exact_replay(store):
    load(store, journey())
    before = snapshot(store)
    load(store, journey())
    after = snapshot(store)
    assert before["fingerprint"] == after["fingerprint"]


def test_identity_collision_cannot_attach_another_users_payment(store):
    load(store, journey()[:-1])
    load(store, [event("paid", "2026-01-05T11:00:00Z", user="attacker", cents=15000)])
    result = snapshot(store)
    assert result["metrics"]["gross_cents"] == 0
    assert result["exclusions"][0]["reason"] == "identity_mismatch"


def test_duplicate_business_payment_with_new_event_id_is_excluded(store):
    load(store, journey())
    load(store, [event("paid", "2026-01-05T11:00:00Z", eid="retry-new-id", cents=10000)])
    result = snapshot(store)
    assert result["metrics"]["paid_rentals"] == 1
    assert result["metrics"]["gross_cents"] == 10000
    assert result["exclusions"][0]["reason"] == "duplicate_payment"


def test_invalid_report_range_is_explicit(store):
    with pytest.raises(ValueError, match="end"):
        report(store, start="2026-02-01", end="2026-01-01")
