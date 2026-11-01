import os
import uuid

import pytest

from productpulse.store import Store


@pytest.fixture
def store(tmp_path):
    url = os.environ.get("PRODUCTPULSE_TEST_DATABASE_URL")
    # Каждый тест PostgreSQL изолирован в собственной схеме; public не удаляется.
    schema = "test_" + uuid.uuid4().hex if url else None
    db = Store(url or f"sqlite:///{tmp_path / 'test.db'}", schema=schema)
    db.initialize()
    yield db
    db.close(drop_schema=bool(schema))


def event(
    kind, at="2026-01-05T10:00:00Z", eid=None, user="u1", session="s1", rental="r1", cents=None
):
    value = {
        "event_id": eid or f"{session}-{kind}",
        "event_type": kind,
        "occurred_at": at,
        "user_id": user,
        "session_id": session,
        "rental_id": rental,
        "channel": "organic",
        "category": "camera",
    }
    if cents is not None:
        value["amount_cents"] = cents
    return value


def journey(day="2026-01-05", user="u1", session="s1", rental="r1", cents=10000):
    return [
        event(
            kind,
            f"{day}T10:0{i}:00Z",
            user=user,
            session=session,
            rental=rental,
            cents=cents if kind == "paid" else None,
        )
        for i, kind in enumerate(["viewed", "quoted", "reserved", "paid"])
    ]
