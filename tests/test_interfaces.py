"""HTTP-контракт: чтение, экспорт, валидация и безопасная диагностика."""

import json

from conftest import journey
from fastapi.testclient import TestClient

from productpulse.api import create_app
from productpulse.cli import main


def client_with_data(store):
    store.ingest(
        [json.dumps(e) for e in journey()], source="тест", received_at="2026-01-10T00:00:00Z"
    )
    return TestClient(create_app(store))


def test_api_report_drilldown_and_local_assets(store):
    with client_with_data(store) as client:
        response = client.get("/api/report", params={"start": "2026-01-01", "end": "2026-02-01"})
        assert response.status_code == 200
        data = response.json()
        assert data["metrics"]["net_cents"] == 10000
        eid = data["ledger"][0]["event_id"]
        receipts = client.get("/api/receipts", params={"event_id": eid}).json()
        assert receipts["items"][0]["payload"]["amount_cents"] == 10000
        page = client.get("/")
        assert page.status_code == 200
        assert 'lang="ru"' in page.text
        assert "Content-Security-Policy" in page.headers
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/api/health").json()["status"] == "ok"


def test_api_invalid_ranges_and_pagination_are_400_or_422(store):
    with TestClient(create_app(store)) as client:
        result = client.get("/api/report", params={"start": "2026-02-01", "end": "2026-01-01"})
        assert result.status_code == 400
        assert "end" in result.json()["detail"]
        assert client.get("/api/receipts?limit=9999").status_code == 422
        assert client.get("/api/receipts?status=anything").status_code == 400
        assert client.post("/api/receipts", json={}).status_code == 405


def test_export_ledger_matches_report_and_neutralizes_formula_cells(store):
    entries = journey()
    for e in entries:
        e["user_id"] = "=1+1"
    store.ingest([json.dumps(e) for e in entries], source="тест")
    with TestClient(create_app(store)) as client:
        csv = client.get("/api/export", params={"start": "2026-01-01", "end": "2026-02-01"})
        assert csv.status_code == 200
        assert "text/csv" in csv.headers["content-type"]
        assert "'=1+1" in csv.text
        assert "10000" in csv.text


def test_api_asof_applies_to_receipts(store):
    with client_with_data(store) as client:
        assert client.get("/api/receipts?as_of=2026-01-09T00:00:00Z").json()["total"] == 0
        assert client.get("/api/receipts?as_of=wrong").status_code == 400


def test_cli_ingest_report_and_invalid_range(tmp_path, capsys):
    db = f"sqlite:///{tmp_path / 'cli.db'}"
    source = tmp_path / "events.jsonl"
    source.write_text("\n".join(json.dumps(e) for e in journey()), encoding="utf-8")
    assert main(["--database", db, "init"]) == 0
    assert (
        main(
            [
                "--database",
                db,
                "ingest",
                str(source),
                "--source",
                "тест",
                "--received-at",
                "2026-01-10T00:00:00Z",
            ]
        )
        == 0
    )
    capsys.readouterr()
    assert main(["--database", db, "report", "--start", "2026-01-01", "--end", "2026-02-01"]) == 0
    assert json.loads(capsys.readouterr().out)["metrics"]["net_cents"] == 10000
    assert main(["--database", db, "report", "--start", "2026-02-01", "--end", "2026-01-01"]) == 2
    assert "end" in capsys.readouterr().err


def test_reference_is_local_and_validation_errors_are_russian(store):
    with TestClient(create_app(store)) as client:
        page = client.get("/docs")
        assert page.status_code == 200
        assert 'lang="ru"' in page.text
        assert "cdn.jsdelivr" not in page.text
        assert "openapi.json" in page.text
        error = client.get("/api/report").json()
        assert error["detail"] == "Проверьте обязательные параметры и допустимые значения."
        assert client.get("/openapi.json").status_code == 200


def test_api_roundtrips_quarantined_raw_surrogate(store):
    store.ingest(["broken\ud800json"], source="тест")
    with TestClient(create_app(store)) as client:
        response = client.get("/api/receipts?status=invalid")
        assert response.status_code == 200
        assert response.json()["items"][0]["raw_json"] == "broken\ud800json"


def test_api_rejects_huge_offset_before_sql(store):
    with TestClient(create_app(store)) as client:
        response = client.get("/api/receipts?offset=9223372036854775808")
        assert response.status_code == 422


def test_cli_help_and_parser_errors_are_russian(capsys):
    import pytest

    with pytest.raises(SystemExit) as helped:
        main(["--help"])
    assert helped.value.code == 0
    help_text = capsys.readouterr().out
    assert "Использование:" in help_text
    assert "Параметры:" in help_text
    assert "show this help" not in help_text
    assert "usage:" not in help_text
    with pytest.raises(SystemExit) as invalid:
        main(["serve", "--port", "wrong"])
    assert invalid.value.code == 2
    message = capsys.readouterr().err
    assert "целое число" in message
    assert "invalid int value" not in message


def test_http_revision_explains_payment_from_canonical_receipts(store):
    entries = journey()
    store.ingest(
        [json.dumps(e) for e in entries], source="тест", received_at="2026-01-08T00:00:00Z"
    )
    with TestClient(create_app(store)) as client:
        response = client.get(
            "/api/compare",
            params={
                "start": "2026-01-01",
                "end": "2026-02-01",
                "before": "2026-01-07T00:00:00Z",
                "after": "2026-01-09T00:00:00Z",
            },
        )
        assert response.status_code == 200
        revision = response.json()["revision"]
        assert revision["ledger_delta"]["net_cents"] == 10000
        row = revision["ledger_changes"][0]
        assert row["reason"] == "new_delivery"
        assert row["before"] is None
        assert len(row["after"]["source_refs"]) == 4
        receipt = client.get("/api/receipts", params={"event_id": row["event_id"]}).json()
        assert receipt["items"][0]["id"] == row["after"]["receipt_id"]


def test_http_archives_are_read_only_and_return_exact_saved_bytes(store, monkeypatch):
    from productpulse import analytics
    from productpulse.snapshots import read_snapshot_bytes, save_snapshot

    store.ingest(
        [json.dumps(e) for e in journey()], source="архив", received_at="2026-01-06T00:00:00Z"
    )
    manifest = save_snapshot(store, start="2026-01-01", end="2026-02-01")
    sid = manifest["id"]
    expected = {fmt: read_snapshot_bytes(store, sid, format=fmt)[1] for fmt in ("json", "csv")}

    def forbidden(*args, **kwargs):
        raise AssertionError("HTTP архива не должен вызывать проектор")

    monkeypatch.setattr(analytics, "_project", forbidden)
    with TestClient(create_app(store)) as client:
        assert client.get("/api/snapshots").json()["total"] == 1
        assert client.get(f"/api/snapshots/{sid}").json() == manifest
        for fmt, suffix, digest in [
            ("json", "report", "report_sha256"),
            ("csv", "ledger.csv", "csv_sha256"),
        ]:
            response = client.get(f"/api/snapshots/{sid}/{suffix}")
            assert response.status_code == 200
            assert response.content == expected[fmt]
            assert response.headers["x-snapshot-id"] == sid
            assert response.headers["x-content-sha256"] == manifest[digest]
        assert client.post("/api/snapshots", json={}).status_code == 405
        assert client.delete(f"/api/snapshots/{sid}").status_code == 405
        assert client.get("/api/snapshots/00000000-0000-4000-8000-000000000000").status_code == 404
        assert client.get("/api/snapshots/invalid").status_code == 400
        assert client.get("/api/snapshots?offset=9223372036854775808").status_code == 422


def test_cli_saves_lists_and_exports_archive_without_overwriting(tmp_path, capsys):
    db = f"sqlite:///{tmp_path / 'archive-cli.db'}"
    assert (
        main(["--database", db, "snapshot-save", "--start", "2026-01-01", "--end", "2026-02-01"])
        == 0
    )
    manifest = json.loads(capsys.readouterr().out)
    assert main(["--database", db, "snapshot-list"]) == 0
    assert json.loads(capsys.readouterr().out)["items"][0]["id"] == manifest["id"]
    target = tmp_path / "snapshot.json"
    args = [
        "--database",
        db,
        "snapshot-export",
        manifest["id"],
        "--format",
        "json",
        "--output",
        str(target),
    ]
    assert main(args) == 0
    original = target.read_bytes()
    assert json.loads(original)["metrics"]["net_cents"] == 0
    assert main(args) == 2
    assert target.read_bytes() == original


def test_http_refuses_corrupt_archive_without_returning_saved_payload(store):
    from uuid import uuid4

    from sqlalchemy import select

    from productpulse.snapshots import save_snapshot

    manifest = save_snapshot(store, start="2026-01-01", end="2026-02-01")
    with store.engine.begin() as conn:
        row = dict(
            conn.execute(select(store.snapshots).where(store.snapshots.c.id == manifest["id"]))
            .mappings()
            .one()
        )
        damaged_id = str(uuid4())
        row.update(id=damaged_id, report_json=b"corrupt-content")
        conn.execute(store.snapshots.insert().values(**row))
    with TestClient(create_app(store)) as client:
        response = client.get(f"/api/snapshots/{damaged_id}/report")
        assert response.status_code == 409
        assert "SHA-256" in response.json()["detail"]
        assert b"corrupt-content" not in response.content
