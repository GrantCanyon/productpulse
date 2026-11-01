"""Детерминированные синтетические доставки и независимый журнал ожидаемых итогов."""

import json
import random
from collections import Counter
from datetime import UTC, datetime, timedelta

from productpulse.analytics import report
from productpulse.domain import iso


def generate(*, seed_value=27, users=240, weeks=8):
    if not 1 <= users <= 10000 or not 1 <= weeks <= 26:
        raise ValueError("users: от 1 до 10000; weeks: от 1 до 26")
    rng = random.Random(seed_value)
    start = datetime(2026, 1, 5, tzinfo=UTC)
    end = start + timedelta(weeks=weeks)
    deliveries, ledger = [], []
    stage_counts = [0, 0, 0, 0]
    channels, categories = (
        ["organic", "search", "partners", "email"],
        ["camera", "audio", "lighting"],
    )
    counter = 0
    for u in range(users):
        channel = rng.choice(channels)
        for w in range(weeks):
            if rng.random() > (0.82 if w == 0 else 0.28):
                continue
            counter += 1
            sid, rid = f"session-{counter:05}", f"rental-{counter:05}"
            at = start + timedelta(weeks=w, days=rng.randrange(6), hours=rng.randrange(8, 20))
            category = rng.choice(categories)
            depth = 1
            for chance in (0.76, 0.72, 0.84):
                if rng.random() > chance:
                    break
                depth += 1
            amount = rng.randrange(35, 420) * 100
            late = rng.random() < 0.13 and depth == 4
            originals = []
            for i, kind in enumerate(("viewed", "quoted", "reserved", "paid")[:depth]):
                stage_counts[i] += 1
                e = {
                    "event_id": f"{sid}-{kind}",
                    "event_type": kind,
                    "occurred_at": iso(at + timedelta(minutes=i * 8)),
                    "user_id": f"user-{u:04}",
                    "session_id": sid,
                    "rental_id": rid,
                    "channel": channel,
                    "category": category,
                }
                if kind == "paid":
                    e["amount_cents"] = amount
                    ledger.append({"event_id": e["event_id"], "amount_cents": amount})
                received = at + timedelta(minutes=45, days=3 if late and i < 3 else 0)
                deliveries.append(
                    {
                        "raw": json.dumps(e, ensure_ascii=False),
                        "received_at": iso(received),
                        "source": "synthetic-checkout",
                    }
                )
                originals.append(e)
            if depth == 4 and rng.random() < 0.16:
                cancel = dict(
                    originals[-1],
                    event_id=f"{sid}-cancelled",
                    event_type="cancelled",
                    occurred_at=iso(at + timedelta(hours=3)),
                )
                del cancel["amount_cents"]
                deliveries.append(
                    {
                        "raw": json.dumps(cancel),
                        "received_at": iso(at + timedelta(hours=4)),
                        "source": "synthetic-refunds",
                    }
                )
                ledger.append({"event_id": cancel["event_id"], "amount_cents": -amount})
            if rng.random() < 0.18:
                deliveries.append(
                    {
                        "raw": json.dumps(originals[-1], indent=2),
                        "received_at": iso(at + timedelta(days=4)),
                        "source": "synthetic-retry",
                    }
                )
            if rng.random() < 0.04:
                collision = dict(originals[-1], user_id="conflicting-identity")
                deliveries.append(
                    {
                        "raw": json.dumps(collision),
                        "received_at": iso(at + timedelta(days=5)),
                        "source": "synthetic-conflict",
                    }
                )
    # Повреждённый JSON и платежи без цепочки — отдельный канал проверки качества.
    deliveries.append(
        {"raw": '{"broken":', "received_at": iso(start), "source": "synthetic-invalid"}
    )
    for i in range(max(1, users // 30)):
        orphan = {
            "event_id": f"orphan-{i}",
            "event_type": "paid",
            "occurred_at": iso(start),
            "user_id": f"orphan-user-{i}",
            "session_id": f"orphan-session-{i}",
            "rental_id": f"orphan-rental-{i}",
            "amount_cents": 15000,
            "channel": "partners",
            "category": "audio",
        }
        deliveries.append(
            {"raw": json.dumps(orphan), "received_at": iso(start), "source": "synthetic-orphans"}
        )
    deliveries.sort(key=lambda item: item["received_at"])
    gross = sum(item["amount_cents"] for item in ledger if item["amount_cents"] > 0)
    reversed_cents = -sum(item["amount_cents"] for item in ledger if item["amount_cents"] < 0)
    return {
        "seed": seed_value,
        "users": users,
        "weeks": weeks,
        "start": str(start.date()),
        "end": str(end.date()),
        "final_as_of": iso(end + timedelta(days=7)),
        "deliveries": deliveries,
        "truth_ledger": ledger,
        "truth": {
            "gross_cents": gross,
            "reversed_cents": reversed_cents,
            "net_cents": gross - reversed_cents,
            "paid_rentals": stage_counts[-1],
            "cancelled_rentals": sum(item["amount_cents"] < 0 for item in ledger),
            "conversion_pct": round(100 * stage_counts[3] / stage_counts[0], 2)
            if stage_counts[0]
            else 0.0,
        },
    }


def seed(store, scenario):
    counts = Counter()
    for delivery in scenario["deliveries"]:
        counts.update(
            store.ingest(
                [delivery["raw"]], source=delivery["source"], received_at=delivery["received_at"]
            )
        )
    snapshot = report(
        store, start=scenario["start"], end=scenario["end"], as_of=scenario["final_as_of"]
    )
    actual = {key: snapshot["metrics"][key] for key in scenario["truth"]}
    return {
        "ingested": dict(counts),
        "expected": scenario["truth"],
        "actual": actual,
        "reconciled": actual == scenario["truth"],
        "fingerprint": snapshot["fingerprint"],
    }
