"""Добавляемое хранилище квитанций; запись сериализована на уровне базы."""

import hashlib
import json
import re
from datetime import UTC, datetime

from sqlalchemy import (
    Column,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    create_engine,
    func,
    inspect,
    select,
    text,
)

from productpulse.domain import canonical, iso, timestamp, validate

MAX_OFFSET = 1_000_000
STATUSES = ("accepted", "duplicate", "conflict", "invalid")


class Store:
    def __init__(self, url="sqlite:///productpulse.db", *, schema=None):
        if schema and not re.fullmatch(r"test_[a-f0-9]{32}", schema):
            raise ValueError("Недопустимая тестовая схема")
        self.schema = schema
        self.engine = create_engine(url, pool_pre_ping=True)
        self.postgres = self.engine.dialect.name == "postgresql"
        if self.engine.dialect.name not in {"postgresql", "sqlite"}:
            raise ValueError("Поддерживаются PostgreSQL и SQLite")
        self.meta = MetaData(schema=schema)
        self.raw = Table(
            "receipts",
            self.meta,
            Column("id", Integer, primary_key=True),
            Column("stored_raw_json", Text, nullable=False),
            Column("raw_sha256", String(64), nullable=False),
            Column("source", String(200), nullable=False),
            Column("received_at", String(32), nullable=False, index=True),
            Column("status", String(16), nullable=False, index=True),
            Column("reason", Text),
            Column("event_id", String(200), index=True),
            Column("payload", Text),
            Column("payload_sha256", String(64)),
        )
        Index(
            "one_accepted_identity",
            self.raw.c.event_id,
            unique=True,
            sqlite_where=self.raw.c.status == "accepted",
            postgresql_where=self.raw.c.status == "accepted",
        )

    def initialize(self):
        with self.engine.begin() as conn:
            if self.schema:
                conn.execute(text(f'CREATE SCHEMA IF NOT EXISTS "{self.schema}"'))
            inspector = inspect(conn)
            if inspector.has_table("receipts", schema=self.schema):
                columns = {
                    column["name"]
                    for column in inspector.get_columns("receipts", schema=self.schema)
                }
                if columns != set(self.raw.c.keys()):
                    raise ValueError(
                        "Несовместимая схема базы. Сохраните старую базу и создайте новую."
                    )
            self.meta.create_all(conn)
            if self.postgres:
                prefix = f'"{self.schema}".' if self.schema else ""
                conn.execute(
                    text(f"""CREATE OR REPLACE FUNCTION {prefix}reject_receipt_change()
                    RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN
                    RAISE EXCEPTION 'Квитанции неизменяемы'; END $$""")
                )
                table = prefix + "receipts"
                exists = conn.execute(
                    text(
                        "SELECT 1 FROM pg_trigger WHERE tgname = "
                        "'immutable_receipts' AND tgrelid = :table::regclass".replace(
                            ":table::", "CAST(:table AS text)::"
                        )
                    ),
                    {"table": table},
                ).scalar()
                if not exists:
                    conn.execute(
                        text(
                            f"CREATE TRIGGER immutable_receipts BEFORE UPDATE OR DELETE "
                            f"ON {table} FOR EACH ROW EXECUTE FUNCTION "
                            f"{prefix}reject_receipt_change()"
                        )
                    )
            else:
                for action in ("UPDATE", "DELETE"):
                    conn.execute(
                        text(
                            f"CREATE TRIGGER IF NOT EXISTS immutable_{action.lower()} "
                            f"BEFORE {action} ON receipts BEGIN "
                            "SELECT RAISE(ABORT, 'Квитанции неизменяемы'); END"
                        )
                    )

    def ingest(self, lines, *, source, received_at=None):
        if not isinstance(source, str) or not source.strip() or len(source) > 200:
            raise ValueError("Источник: обязательная строка до 200 символов")
        if any(0xD800 <= ord(char) <= 0xDFFF or ord(char) == 0 for char in source):
            raise ValueError("Источник содержит недопустимый символ Unicode")
        received = iso(timestamp(received_at) if received_at else datetime.now(UTC))
        counts = dict.fromkeys(STATUSES, 0)
        with self.engine.connect() as conn:
            if self.postgres:
                conn.begin()
                conn.execute(text("SELECT pg_advisory_xact_lock(739167823)"))
            else:
                conn.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                for raw in lines:
                    if not isinstance(raw, str):
                        raise ValueError("Входной поток должен содержать строки")
                    record = {
                        "stored_raw_json": json.dumps(raw, ensure_ascii=True),
                        "raw_sha256": hashlib.sha256(
                            raw.encode("utf-8", "surrogatepass")
                        ).hexdigest(),
                        "source": source,
                        "received_at": received,
                        "status": "invalid",
                        "reason": None,
                    }
                    try:
                        payload = validate(raw)
                    except ValueError as exc:
                        record["reason"] = str(exc)
                    else:
                        encoded = canonical(payload)
                        digest = hashlib.sha256(encoded.encode()).hexdigest()
                        previous = conn.execute(
                            select(self.raw.c.payload_sha256).where(
                                self.raw.c.event_id == payload["event_id"],
                                self.raw.c.status == "accepted",
                            )
                        ).scalar_one_or_none()
                        status = (
                            "accepted"
                            if previous is None
                            else ("duplicate" if previous == digest else "conflict")
                        )
                        record.update(
                            status=status,
                            event_id=payload["event_id"],
                            payload=encoded,
                            payload_sha256=digest,
                        )
                        if status == "conflict":
                            record["reason"] = "event_id уже принят с другим содержимым"
                        elif status == "duplicate":
                            record["reason"] = "Повтор эквивалентного события"
                    conn.execute(self.raw.insert().values(**record))
                    counts[record["status"]] += 1
                conn.commit()
            except BaseException:
                conn.rollback()
                raise
        return counts

    def read(self, *, as_of=None):
        query = select(self.raw).order_by(self.raw.c.id)
        if as_of:
            query = query.where(self.raw.c.received_at <= iso(timestamp(as_of)))
        with self.engine.connect() as conn:
            items = [dict(row) for row in conn.execute(query).mappings()]
        for item in items:
            item["raw_json"] = json.loads(item.pop("stored_raw_json"))
        return items

    def receipts(self, *, status=None, event_id=None, as_of=None, offset=0, limit=50):
        if status and status not in STATUSES:
            raise ValueError("Неизвестный статус квитанции")
        if type(offset) is not int or not 0 <= offset <= MAX_OFFSET or not 1 <= limit <= 200:
            raise ValueError("Пагинация: offset от 0 до 1000000, limit от 1 до 200")
        clauses = []
        if status:
            clauses.append(self.raw.c.status == status)
        if event_id:
            clauses.append(self.raw.c.event_id == event_id)
        if as_of:
            clauses.append(self.raw.c.received_at <= iso(timestamp(as_of)))
        with self.engine.connect() as conn:
            total = conn.execute(
                select(func.count()).select_from(self.raw).where(*clauses)
            ).scalar()
            rows = conn.execute(
                select(self.raw)
                .where(*clauses)
                .order_by(self.raw.c.id.desc())
                .offset(offset)
                .limit(limit)
            ).mappings()
            items = [dict(row) for row in rows]
        for item in items:
            item["raw_json"] = json.loads(item.pop("stored_raw_json"))
            item["payload"] = json.loads(item["payload"]) if item["payload"] else None
        return {"total": total, "offset": offset, "limit": limit, "items": items}

    def close(self, *, drop_schema=False):
        if drop_schema and self.schema:
            with self.engine.begin() as conn:
                conn.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.engine.dispose()
