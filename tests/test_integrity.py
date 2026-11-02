"""Регрессии контракта хранения и независимой контрольной суммы симулятора."""

import json
from concurrent.futures import ThreadPoolExecutor

import pytest
from conftest import event, journey
from sqlalchemy.exc import DBAPIError

from productpulse.analytics import compare, report
from productpulse.simulator import generate, seed


def test_database_rejects_receipt_mutation(store):
    store.ingest([json.dumps(event("viewed"))], source="тест")
    for statement in (store.raw.update().values(source="подмена"), store.raw.delete()):
        with pytest.raises(DBAPIError), store.engine.begin() as conn:
            conn.execute(statement)
    assert store.receipts()["total"] == 1


def test_concurrent_replay_has_one_winner(store):
    raw = json.dumps(event("viewed"))
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(lambda _: store.ingest([raw], source="параллельно"), range(8)))
    assert sum(item["accepted"] for item in results) == 1
    assert sum(item["duplicate"] for item in results) == 7
    assert store.receipts()["total"] == 8


def test_equivalent_json_and_timezone_are_duplicate(store):
    first = event("viewed")
    second = dict(first, occurred_at="2026-01-05T11:00:00+01:00")
    result = store.ingest([json.dumps(first), json.dumps(second, indent=2)], source="тест")
    assert result["duplicate"] == 1


def test_duplicate_keys_and_unknown_fields_invalid(store):
    raw = json.dumps(event("viewed"))
    bad = raw[:-1] + ', "event_id":"подмена"}'
    result = store.ingest(
        [bad, json.dumps(dict(event("viewed"), secret="неизвестно"))], source="тест"
    )
    assert result["invalid"] == 2


def test_generator_is_reproducible_and_reconciles_independent_truth(store):
    scenario = generate(seed_value=42, users=80, weeks=6)
    assert scenario == generate(seed_value=42, users=80, weeks=6)
    assert scenario != generate(seed_value=43, users=80, weeks=6)
    outcome = seed(store, scenario)
    actual = report(
        store, start=scenario["start"], end=scenario["end"], as_of=scenario["final_as_of"]
    )
    for key, expected in scenario["truth"].items():
        assert actual["metrics"][key] == expected, key
    assert actual["quality"]["invalid"] > 0
    assert actual["quality"]["conflict"] > 0
    assert actual["quality"]["duplicate"] > 0
    assert outcome["reconciled"] is True
    before = actual["fingerprint"]
    seed(store, scenario)
    assert (
        report(store, start=scenario["start"], end=scenario["end"], as_of=scenario["final_as_of"])[
            "fingerprint"
        ]
        == before
    )


def test_partial_cohort_week_is_marked_observed_but_not_mature(store):
    store.ingest(
        [json.dumps(e) for e in journey()], source="тест", received_at="2026-01-06T00:00:00Z"
    )
    result = report(store, start="2026-01-01", end="2026-01-15")
    assert result["cohorts"][0]["observed"][:3] == [True, True, False]
    assert result["cohorts"][0]["mature"][:3] == [True, False, False]


def test_compare_explains_late_chain_revision(store):
    entries = journey()
    store.ingest([json.dumps(entries[-1])], source="тест", received_at="2026-01-06T00:00:00Z")
    store.ingest(
        [json.dumps(e) for e in entries[:-1]], source="тест", received_at="2026-01-08T00:00:00Z"
    )
    diff = compare(
        store,
        start="2026-01-01",
        end="2026-02-01",
        before="2026-01-07T00:00:00Z",
        after="2026-01-09T00:00:00Z",
    )
    assert diff["delta"]["net_cents"] == 10000
    assert set(diff["added_event_ids"]) == {e["event_id"] for e in entries}


def test_provenance_contains_every_required_predecessor(store):
    entries = journey()
    store.ingest([json.dumps(e) for e in entries], source="тест")
    ledger = report(store, start="2026-01-01", end="2026-02-01")["ledger"]
    assert ledger[0]["source_event_ids"] == [e["event_id"] for e in entries]


def test_asof_is_ingestion_axis_not_an_event_time_ceiling(store):
    entries = journey(day="2026-01-10")
    store.ingest(
        [json.dumps(e) for e in entries],
        source="ранняя доставка",
        received_at="2026-01-06T00:00:00Z",
    )
    data = report(store, start="2026-01-01", end="2026-02-01", as_of="2026-01-07T00:00:00Z")
    assert data["metrics"]["gross_cents"] == 10000
    assert data["cohorts"][0]["mature"][:3] == [True, True, True]
    assert data["cohorts"][0]["observed"][3] is True
    assert data["cohorts"][0]["mature"][3] is False


@pytest.mark.parametrize(
    "mutate",
    [
        lambda e: dict(e, user_id="\ud800"),
        lambda e: dict(e, occurred_at="9999-12-31T23:59:59-01:00"),
    ],
)
def test_invalid_unicode_and_overflow_do_not_rollback_valid_neighbors(store, mutate):
    first = event("viewed", eid="good-before", session="a", rental="a")
    last = event("viewed", eid="good-after", session="b", rental="b")
    bad = mutate(event("viewed", eid="bad"))
    result = store.ingest([json.dumps(first), json.dumps(bad), json.dumps(last)], source="тест")
    assert result == {"accepted": 2, "duplicate": 0, "conflict": 0, "invalid": 1}
    assert store.receipts()["total"] == 3


def test_segment_fingerprint_and_lineage_ignore_unrelated_activity(store):
    entries = journey()
    store.ingest(
        [json.dumps(e) for e in entries], source="тест", received_at="2026-01-10T00:00:00Z"
    )
    before = report(store, start="2026-01-01", end="2026-02-01", channel="organic")
    unrelated = [dict(e, channel="search") for e in journey(user="u2", session="s2", rental="r2")]
    store.ingest(
        [json.dumps(e) for e in unrelated], source="тест", received_at="2026-01-12T00:00:00Z"
    )
    after = report(store, start="2026-01-01", end="2026-02-01", channel="organic")
    assert before["metrics"] == after["metrics"]
    assert before["fingerprint"] == after["fingerprint"]
    assert before["source_event_ids"] == after["source_event_ids"]
    assert {e["event_id"] for e in unrelated} <= set(after["context_event_ids"])
    change = compare(
        store,
        start="2026-01-01",
        end="2026-02-01",
        channel="organic",
        before="2026-01-11T00:00:00Z",
        after="2026-01-13T00:00:00Z",
    )
    assert change["added_event_ids"] == []
    assert change["removed_event_ids"] == []


@pytest.mark.parametrize(
    "raw", ["broken\x00json", "broken\ud800json", json.dumps(event("viewed", eid="bad\x00id"))]
)
def test_raw_storage_preserves_unrepresentable_text_without_losing_neighbors(store, raw):
    import hashlib

    result = store.ingest(
        [json.dumps(event("viewed", eid="first")), raw, json.dumps(event("viewed", eid="last"))],
        source="тест",
    )
    assert result == {"accepted": 2, "duplicate": 0, "conflict": 0, "invalid": 1}
    receipt = store.receipts(status="invalid")["items"][0]
    assert receipt["raw_json"] == raw
    assert receipt["raw_sha256"] == hashlib.sha256(raw.encode("utf-8", "surrogatepass")).hexdigest()


@pytest.mark.parametrize("source", ["bad\x00source", "bad\ud800source"])
def test_invalid_source_rejected_before_any_write(store, source):
    with pytest.raises(ValueError):
        store.ingest([json.dumps(event("viewed"))], source=source)
    assert store.receipts()["total"] == 0


def test_legacy_database_schema_is_rejected_explicitly(tmp_path):
    from sqlalchemy import create_engine, text

    from productpulse.store import Store

    url = f"sqlite:///{tmp_path / 'legacy.db'}"
    engine = create_engine(url)
    with engine.begin() as conn:
        conn.execute(text("CREATE TABLE receipts (id INTEGER PRIMARY KEY, raw_json TEXT)"))
    engine.dispose()
    db = Store(url)
    try:
        with pytest.raises(ValueError, match="схема"):
            db.initialize()
    finally:
        db.close()


@pytest.mark.parametrize("opening,closing", [("[", "]"), ('{"nested":', "}")])
def test_deeply_nested_invalid_json_does_not_abort_neighbors(store, opening, closing):
    import hashlib

    # Предел парсера зависит от сборки: важен карантин, а не конкретный путь отказа.
    raw = opening * 5000 + "0" + closing * 5000
    result = store.ingest(
        [json.dumps(event("viewed", eid="first")), raw, json.dumps(event("viewed", eid="last"))],
        source="тест",
    )
    assert result == {"accepted": 2, "duplicate": 0, "conflict": 0, "invalid": 1}
    assert store.receipts()["total"] == 3
    receipt = store.receipts(status="invalid")["items"][0]
    assert receipt["raw_json"] == raw
    assert receipt["raw_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert receipt["reason"]


def test_parser_recursion_error_is_quarantined_with_neighbors(store, monkeypatch):
    import hashlib

    from productpulse import domain

    raw = json.dumps(event("viewed", eid="parser-limit-fixture"))
    original_loads = json.loads

    def loads_with_injected_limit(value, *args, **kwargs):
        # Инъекция только для маркера; соседей и сохранённые обёртки разбирает настоящий JSON.
        if value == raw:
            raise RecursionError("Синтетический отказ парсера")
        return original_loads(value, *args, **kwargs)

    monkeypatch.setattr(domain.json, "loads", loads_with_injected_limit)
    with pytest.raises(ValueError, match="вложенность") as error:
        domain.validate(raw)
    assert isinstance(error.value.__cause__, RecursionError)
    result = store.ingest(
        [json.dumps(event("viewed", eid="first")), raw, json.dumps(event("viewed", eid="last"))],
        source="инъекция ошибки",
    )
    assert result == {"accepted": 2, "duplicate": 0, "conflict": 0, "invalid": 1}
    assert store.receipts()["total"] == 3
    receipt = store.receipts(status="invalid")["items"][0]
    assert receipt["raw_json"] == raw
    assert receipt["raw_sha256"] == hashlib.sha256(raw.encode("utf-8")).hexdigest()
    assert "вложенность" in receipt["reason"]


def test_store_rejects_huge_offset_before_sql(store):
    with pytest.raises(ValueError, match="Пагинация"):
        store.receipts(offset=9223372036854775808)


@pytest.mark.parametrize(
    "day,start,end",
    [
        ("0001-01-05", "0001-01-01", "0001-01-15"),
        ("9999-12-20", "9999-12-01", "9999-12-31"),
    ],
)
def test_cohorts_support_calendar_boundaries_without_overflow(store, day, start, end):
    store.ingest(
        [json.dumps(e) for e in journey(day=day)], source="границы", received_at=f"{day}T12:00:00Z"
    )
    data = report(store, start=start, end=end)
    assert data["metrics"]["gross_cents"] == 10000
    cohort = data["cohorts"][0]
    assert cohort["retention"][0] == 100.0
    assert cohort["observed"][0] is True
    assert cohort["mature"][0] is True
    assert cohort["observed"][2:] == [False] * 6
    assert cohort["mature"][2:] == [False] * 6
    assert cohort["retention"][2:] == [None] * 6
