"""Неизменяемые результаты расчёта: одна запись, затем только проверка и чтение байтов."""

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError

from productpulse import analytics
from productpulse.domain import canonical, iso
from productpulse.exports import ledger_csv
from productpulse.store import MAX_OFFSET

FORMAT_VERSION = "snapshot-v1"
METADATA_COLUMNS = (
    "id",
    "created_at",
    "format_version",
    "policy_version",
    "parameters_json",
    "semantic_fingerprint",
    "report_sha256",
    "csv_sha256",
)


class SnapshotNotFound(LookupError):
    """Искомый архив отсутствует; это не повод пересчитывать живой отчёт."""


class SnapshotIntegrityError(RuntimeError):
    """Контрольная сумма сохранённого содержимого не совпала."""


def _manifest(row):
    result = {key: row[key] for key in METADATA_COLUMNS if key != "parameters_json"}
    result["parameters"] = json.loads(row["parameters_json"])
    return result


def save_snapshot(store, *, start, end, as_of=None, channel=None, category=None):
    # Один захват обеспечивает согласованность JSON и CSV даже при параллельном импорте.
    captured = store.read(as_of=as_of)
    snapshot = analytics._project(
        captured, start=start, end=end, as_of=as_of, channel=channel, category=category
    ).snapshot
    report_bytes = canonical(snapshot).encode("utf-8")
    csv_bytes = ("\ufeff" + ledger_csv(snapshot)).encode("utf-8")
    record = {
        "id": str(uuid4()),
        "created_at": iso(datetime.now(UTC)),
        "format_version": FORMAT_VERSION,
        "policy_version": snapshot["policy_version"],
        "parameters_json": canonical(snapshot["window"]),
        "semantic_fingerprint": snapshot["fingerprint"],
        "report_json": report_bytes,
        "ledger_csv": csv_bytes,
        "report_sha256": hashlib.sha256(report_bytes).hexdigest(),
        "csv_sha256": hashlib.sha256(csv_bytes).hexdigest(),
    }
    try:
        with store.engine.begin() as conn:
            conn.execute(store.snapshots.insert().values(**record))
    except IntegrityError as exc:
        raise ValueError("Архив с таким UUID уже существует; перезапись запрещена") from exc
    return _manifest(record)


def list_snapshots(store, *, offset=0, limit=20):
    if (
        type(offset) is not int
        or not 0 <= offset <= MAX_OFFSET
        or type(limit) is not int
        or not 1 <= limit <= 200
    ):
        raise ValueError("Пагинация: offset от 0 до 1000000, limit от 1 до 200")
    table = store.snapshots
    with store.engine.connect() as conn:
        total = conn.execute(select(func.count()).select_from(table)).scalar()
        rows = conn.execute(
            select(*(table.c[key] for key in METADATA_COLUMNS))
            .order_by(table.c.created_at.desc(), table.c.id.desc())
            .offset(offset)
            .limit(limit)
        ).mappings()
        items = [_manifest(row) for row in rows]
    return {"total": total, "offset": offset, "limit": limit, "items": items}


def _verified_record(store, snapshot_id):
    try:
        identifier = str(UUID(str(snapshot_id)))
    except (ValueError, AttributeError) as exc:
        raise ValueError("Идентификатор архива должен быть UUID") from exc
    with store.engine.connect() as conn:
        record = (
            conn.execute(select(store.snapshots).where(store.snapshots.c.id == identifier))
            .mappings()
            .one_or_none()
        )
    if record is None:
        raise SnapshotNotFound("Архивный снимок не найден")
    record = dict(record)
    for content, digest in (("report_json", "report_sha256"), ("ledger_csv", "csv_sha256")):
        record[content] = bytes(record[content])
        if hashlib.sha256(record[content]).hexdigest() != record[digest]:
            raise SnapshotIntegrityError("Нарушена целостность архива: SHA-256 не совпадает")
    return record


def get_snapshot(store, snapshot_id):
    return _manifest(_verified_record(store, snapshot_id))


def read_snapshot_bytes(store, snapshot_id, *, format="json"):
    if format not in {"json", "csv"}:
        raise ValueError("Формат архива должен быть json или csv")
    record = _verified_record(store, snapshot_id)
    return _manifest(record), record["report_json" if format == "json" else "ledger_csv"]


def export_snapshot(store, snapshot_id, *, format, output: Path):
    manifest, content = read_snapshot_bytes(store, snapshot_id, format=format)
    # xb сохраняет прежние файлы; отказ записи может оставить только новый частичный файл.
    with Path(output).open("xb") as stream:
        stream.write(content)
    return manifest
