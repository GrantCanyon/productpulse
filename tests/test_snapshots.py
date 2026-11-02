"""Архив хранит точные артефакты, независимо от будущей истории и проектора."""

import csv
import hashlib
import io
import json
from uuid import UUID

import pytest
from conftest import journey
from sqlalchemy import text
from sqlalchemy.exc import DBAPIError

from productpulse import analytics, snapshots
from productpulse.analytics import report
from productpulse.snapshots import (
    SnapshotIntegrityError,
    SnapshotNotFound,
    export_snapshot,
    get_snapshot,
    list_snapshots,
    read_snapshot_bytes,
    save_snapshot,
)

FILTERS = {"start": "2026-01-01", "end": "2026-02-01", "as_of": "2026-01-09T00:00:00Z"}


def load(store, events):
    store.ingest(
        [json.dumps(e) for e in events], source="архив", received_at="2026-01-06T00:00:00Z"
    )


def test_snapshot_stays_byte_exact_after_backdated_ingest(store):
    load(store, journey())
    manifest = save_snapshot(store, **FILTERS)
    sid = manifest["id"]
    assert str(UUID(sid)) == sid
    assert manifest["format_version"] == "snapshot-v1"
    assert manifest["policy_version"] == "rental-v1"
    _, json_bytes = read_snapshot_bytes(store, sid, format="json")
    _, csv_bytes = read_snapshot_bytes(store, sid, format="csv")
    assert not json_bytes.startswith(b"\xef\xbb\xbf")
    assert csv_bytes.startswith(b"\xef\xbb\xbf")
    assert manifest["report_sha256"] == hashlib.sha256(json_bytes).hexdigest()
    assert manifest["csv_sha256"] == hashlib.sha256(csv_bytes).hexdigest()
    assert json.loads(json_bytes)["metrics"]["net_cents"] == 10000
    ledger = list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig"))))
    assert sum(int(row["amount_cents"]) for row in ledger) == 10000
    load(store, journey(session="s2", rental="r2", cents=7000))
    assert report(store, **FILTERS)["metrics"]["net_cents"] == 17000
    assert read_snapshot_bytes(store, sid, format="json")[1] == json_bytes
    assert read_snapshot_bytes(store, sid, format="csv")[1] == csv_bytes
    assert get_snapshot(store, sid) == manifest


def test_export_never_calls_changed_projector(store, monkeypatch, tmp_path):
    load(store, journey())
    sid = save_snapshot(store, **FILTERS)["id"]
    expected = {fmt: read_snapshot_bytes(store, sid, format=fmt)[1] for fmt in ("json", "csv")}

    def forbidden(*args, **kwargs):
        raise AssertionError("Архив нельзя пересчитывать")

    monkeypatch.setattr(analytics, "_project", forbidden)
    monkeypatch.setattr(analytics, "report", forbidden)
    for fmt in ("json", "csv"):
        target = tmp_path / f"saved.{fmt}"
        export_snapshot(store, sid, format=fmt, output=target)
        assert target.read_bytes() == expected[fmt]
        assert read_snapshot_bytes(store, sid, format=fmt)[1] == expected[fmt]


def test_snapshot_json_and_csv_share_one_captured_history(store, monkeypatch):
    original = store.read
    reads = 0

    def capture_then_backdate(**kwargs):
        nonlocal reads
        reads += 1
        rows = original(**kwargs)
        load(store, journey())
        return rows

    monkeypatch.setattr(store, "read", capture_then_backdate)
    sid = save_snapshot(store, **FILTERS)["id"]
    assert reads == 1
    result = json.loads(read_snapshot_bytes(store, sid, format="json")[1])
    csv_bytes = read_snapshot_bytes(store, sid, format="csv")[1]
    assert result["metrics"]["net_cents"] == 0
    assert list(csv.DictReader(io.StringIO(csv_bytes.decode("utf-8-sig")))) == []
    assert original()


def test_snapshot_uuid_collision_does_not_replace_existing_archive(store, monkeypatch):
    fixed = UUID("01234567-89ab-4cde-8123-456789abcdef")
    monkeypatch.setattr(snapshots, "uuid4", lambda: fixed)
    sid = save_snapshot(store, **FILTERS)["id"]
    before = read_snapshot_bytes(store, sid, format="json")
    load(store, journey())
    with pytest.raises(ValueError, match="уже существует"):
        save_snapshot(store, **FILTERS)
    assert list_snapshots(store)["total"] == 1
    assert read_snapshot_bytes(store, sid, format="json") == before


def test_snapshot_update_and_delete_are_blocked_by_database(store):
    save_snapshot(store, **FILTERS)
    for mutation in (
        store.snapshots.update().values(policy_version="подмена"),
        store.snapshots.delete(),
    ):
        with pytest.raises(DBAPIError), store.engine.begin() as conn:
            conn.execute(mutation)
    assert list_snapshots(store)["total"] == 1


def test_corrupt_archive_fails_closed_instead_of_recomputing(store):
    sid = save_snapshot(store, **FILTERS)["id"]
    # Только временная тестовая база: имитируем повреждение администратором вне приложения.
    with store.engine.begin() as conn:
        if store.postgres:
            conn.execute(text(f'ALTER TABLE "{store.schema}".snapshots DISABLE TRIGGER USER'))
        else:
            conn.execute(text("DROP TRIGGER immutable_snapshots_update"))
        conn.execute(
            store.snapshots.update()
            .where(store.snapshots.c.id == sid)
            .values(ledger_csv="повреждено".encode())
        )
    with pytest.raises(SnapshotIntegrityError):
        read_snapshot_bytes(store, sid, format="csv")
    with pytest.raises(SnapshotIntegrityError):
        read_snapshot_bytes(store, sid, format="json")


def test_archive_metadata_pagination_and_missing_ids(store):
    first = save_snapshot(store, **FILTERS)
    second = save_snapshot(store, **FILTERS)
    page = list_snapshots(store, limit=1)
    assert page["total"] == 2
    assert len(page["items"]) == 1
    assert "report_json" not in page["items"][0]
    assert {page["items"][0]["id"], list_snapshots(store, offset=1, limit=1)["items"][0]["id"]} == {
        first["id"],
        second["id"],
    }
    with pytest.raises(ValueError):
        list_snapshots(store, offset=2**63)
    with pytest.raises(ValueError):
        get_snapshot(store, "../not-a-uuid")
    with pytest.raises(SnapshotNotFound):
        get_snapshot(store, "00000000-0000-4000-8000-000000000000")


def test_file_export_is_exclusive_and_preserves_existing_file(store, tmp_path):
    sid = save_snapshot(store, **FILTERS)["id"]
    target = tmp_path / "existing.csv"
    target.write_bytes(b"original")
    with pytest.raises(FileExistsError):
        export_snapshot(store, sid, format="csv", output=target)
    assert target.read_bytes() == b"original"
    with pytest.raises(ValueError):
        read_snapshot_bytes(store, sid, format="unknown")
