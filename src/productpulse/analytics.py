"""Единая семантика показателей для CLI, API и панели."""

import hashlib
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import timedelta

from productpulse.domain import canonical, iso, timestamp
from productpulse.revisions import explain_revision
from productpulse.store import STATUSES

POLICY = "rental-v1"
STAGES = ("viewed", "quoted", "reserved", "paid")


def week(value):
    return (value - timedelta(days=value.weekday())).date()


@dataclass
class Projection:
    snapshot: dict
    funnel_memberships: dict


def report(store, *, start, end, as_of=None, channel=None, category=None):
    return _project(
        store.read(as_of=as_of),
        start=start,
        end=end,
        as_of=as_of,
        channel=channel,
        category=category,
    ).snapshot


def _project(rows, *, start, end, as_of=None, channel=None, category=None):
    begin, finish = timestamp(start, date_allowed=True), timestamp(end, date_allowed=True)
    if begin >= finish:
        raise ValueError("end должен быть позже start")
    cutoff = timestamp(as_of) if as_of else None
    if cutoff:
        rows = [row for row in rows if row["received_at"] <= iso(cutoff)]
    quality = dict.fromkeys(STATUSES, 0)
    accepted = []
    for row in rows:
        quality[row["status"]] += 1
        if row["status"] == "accepted":
            value = json.loads(row["payload"])
            value["receipt_id"] = row["id"]
            accepted.append(value)
    accepted.sort(
        key=lambda e: (
            e["occurred_at"],
            STAGES.index(e["event_type"]) if e["event_type"] in STAGES else 4,
            e["event_id"],
        )
    )
    sessions, payments, cancellations = {}, {}, set()
    exclusions, ledger = [], []

    def exclude(e, reason):
        exclusions.append(
            {
                "event_id": e["event_id"],
                "receipt_id": e["receipt_id"],
                "reason": reason,
                "event_type": e["event_type"],
            }
        )

    for e in accepted:
        at = timestamp(e["occurred_at"])
        if at >= finish:
            continue
        sid, kind, rid = e["session_id"], e["event_type"], e["rental_id"]
        state = sessions.get(sid)
        identity = tuple(e[k] for k in ("user_id", "rental_id", "channel", "category"))
        if state and state["identity"] != identity:
            exclude(e, "identity_mismatch")
            continue
        if kind == "viewed":
            if not state:
                sessions[sid] = {"identity": identity, "events": [e]}
            else:
                exclude(e, "repeated_stage")
            continue
        if kind == "cancelled":
            payment = payments.get(rid)
            if not payment:
                exclude(e, "missing_predecessor")
            elif tuple(payment[k] for k in ("user_id", "session_id", "channel", "category")) != (
                e["user_id"],
                sid,
                e["channel"],
                e["category"],
            ):
                exclude(e, "identity_mismatch")
            elif rid in cancellations:
                exclude(e, "duplicate_cancellation")
            else:
                cancellations.add(rid)
                ledger.append(
                    _entry(
                        e,
                        -payment["amount_cents"],
                        payment["event_id"],
                        [x["event_id"] for x in state["events"]] + [e["event_id"]],
                    )
                )
            continue
        if kind == "paid" and rid in payments:
            exclude(e, "duplicate_payment")
            continue
        stage = STAGES.index(kind)
        if not state or len(state["events"]) < stage:
            exclude(e, "missing_predecessor")
        elif len(state["events"]) > stage:
            exclude(e, "repeated_stage")
        else:
            state["events"].append(e)
            if kind == "paid":
                payments[rid] = e
                ledger.append(
                    _entry(
                        e,
                        e["amount_cents"],
                        e["event_id"],
                        [x["event_id"] for x in state["events"]],
                    )
                )

    def matches(e):
        return (not channel or e["channel"] == channel) and (
            not category or e["category"] == category
        )

    window_ledger = [
        e for e in ledger if begin <= timestamp(e["occurred_at"]) < finish and matches(e)
    ]
    gross = sum(e["amount_cents"] for e in window_ledger if e["amount_cents"] > 0)
    reversed_cents = -sum(e["amount_cents"] for e in window_ledger if e["amount_cents"] < 0)
    paid_count = sum(e["amount_cents"] > 0 for e in window_ledger)
    contributing = {eid for entry in window_ledger for eid in entry["source_event_ids"]}
    counts = [0] * 4
    funnel_memberships = {stage: {} for stage in STAGES}
    for state in sessions.values():
        first = state["events"][0]
        if begin <= timestamp(first["occurred_at"]) < finish and matches(first):
            contributing.update(e["event_id"] for e in state["events"])
            for i in range(len(state["events"])):
                counts[i] += 1
                funnel_memberships[STAGES[i]][first["session_id"]] = [
                    e["event_id"] for e in state["events"][: i + 1]
                ]
    funnel = [
        {
            "stage": s,
            "sessions": counts[i],
            "from_previous_pct": round(100 * counts[i] / counts[i - 1], 2)
            if i and counts[i - 1]
            else None,
        }
        for i, s in enumerate(STAGES)
    ]
    daily = defaultdict(lambda: {"gross_cents": 0, "reversed_cents": 0, "net_cents": 0})
    for entry in window_ledger:
        row = daily[entry["occurred_at"][:10]]
        row["net_cents"] += entry["amount_cents"]
        row["gross_cents" if entry["amount_cents"] > 0 else "reversed_cents"] += abs(
            entry["amount_cents"]
        )
    # Когорта определяется полной историей, затем применяется фильтр к платежам.
    user_payments = defaultdict(list)
    for e in payments.values():
        user_payments[e["user_id"]].append(e)
    cohorts = defaultdict(list)
    for user, history in user_payments.items():
        first = min(history, key=lambda e: e["occurred_at"])
        if begin <= timestamp(first["occurred_at"]) < finish and matches(first):
            cohorts[week(timestamp(first["occurred_at"]))].append((user, history))
            for payment in history:
                if matches(payment):
                    contributing.update(
                        e["event_id"] for e in sessions[payment["session_id"]]["events"]
                    )
    cohort_rows = []
    end_day = finish.date().toordinal()
    end_has_time = bool(finish.hour or finish.minute or finish.second or finish.microsecond)
    for cohort_week, users in sorted(cohorts.items()):
        retention, observed, mature = [], [], []
        for n in range(8):
            target = cohort_week.toordinal() + 7 * n
            observed.append(target < end_day or (target == end_day and end_has_time))
            mature.append(target + 7 <= end_day)
            if not observed[-1]:
                retention.append(None)
            else:
                active = sum(
                    any(
                        week(timestamp(e["occurred_at"])).toordinal() == target and matches(e)
                        for e in history
                    )
                    for _, history in users
                )
                retention.append(round(100 * active / len(users), 2))
        cohort_rows.append(
            {
                "week": str(cohort_week),
                "users": len(users),
                "retention": retention,
                "observed": observed,
                "mature": mature,
            }
        )
    sources = sorted(contributing)
    context_ids = sorted(
        e["event_id"]
        for e in accepted
        if timestamp(e["occurred_at"]) < finish and e["event_id"] not in contributing
    )
    metrics = {
        "gross_cents": gross,
        "reversed_cents": reversed_cents,
        "net_cents": gross - reversed_cents,
        "paid_rentals": paid_count,
        "cancelled_rentals": sum(e["amount_cents"] < 0 for e in window_ledger),
        "conversion_pct": round(100 * counts[3] / counts[0], 2) if counts[0] else 0.0,
    }
    daily_rows = [{"date": day, **values} for day, values in sorted(daily.items())]
    content = {
        "policy_version": POLICY,
        "start": iso(begin),
        "end": iso(finish),
        "channel": channel,
        "category": category,
        "events": [
            {k: v for k, v in e.items() if k != "receipt_id"}
            for e in accepted
            if e["event_id"] in contributing
        ],
        "metrics": metrics,
        "funnel": funnel,
        "cohorts": cohort_rows,
        "daily": daily_rows,
    }
    snapshot = {
        "policy_version": POLICY,
        "window": {
            "start": iso(begin),
            "end": iso(finish),
            "as_of": iso(cutoff) if cutoff else None,
            "channel": channel,
            "category": category,
        },
        "fingerprint": hashlib.sha256(canonical(content).encode()).hexdigest(),
        "source_event_ids": sources,
        "context_event_ids": context_ids,
        "metrics": metrics,
        "funnel": funnel,
        "daily": daily_rows,
        "cohorts": cohort_rows,
        "ledger": window_ledger,
        "quality": quality,
        "exclusions": exclusions,
        "exclusion_counts": dict(Counter(e["reason"] for e in exclusions)),
        "dimensions": {
            "channels": sorted({e["channel"] for e in accepted}),
            "categories": sorted({e["category"] for e in accepted}),
        },
    }

    return Projection(snapshot, funnel_memberships)


def _entry(e, amount, payment_id, source_ids):
    return {
        key: e[key]
        for key in (
            "event_id",
            "receipt_id",
            "occurred_at",
            "user_id",
            "session_id",
            "rental_id",
            "channel",
            "category",
        )
    } | {"amount_cents": amount, "payment_event_id": payment_id, "source_event_ids": source_ids}


def compare(store, *, before, after, **filters):
    if timestamp(before) > timestamp(after):
        raise ValueError("after должен быть не раньше before")
    # Одна захваченная история: импорт между проекциями не смешивает два состояния базы.
    rows = store.read(as_of=after)
    left_projection = _project(rows, as_of=before, **filters)
    right_projection = _project(rows, as_of=after, **filters)
    left, right = left_projection.snapshot, right_projection.snapshot
    old_ids, new_ids = set(left["source_event_ids"]), set(right["source_event_ids"])
    return {
        "before": left,
        "after": right,
        "delta": {
            key: round(right["metrics"][key] - value, 2) for key, value in left["metrics"].items()
        },
        "added_event_ids": sorted(new_ids - old_ids),
        "removed_event_ids": sorted(old_ids - new_ids),
        "revision": explain_revision(
            rows,
            left,
            right,
            left_projection.funnel_memberships,
            right_projection.funnel_memberships,
        ),
    }
