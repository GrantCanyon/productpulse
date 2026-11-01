"""Разница журналов и воронок; причины выводятся только из сохранённых фактов."""

import json

MONEY_FIELDS = ("gross_cents", "reversed_cents", "net_cents")


def _money(entry):
    amount = entry["amount_cents"] if entry else 0
    return {"gross_cents": max(amount, 0), "reversed_cents": max(-amount, 0), "net_cents": amount}


def explain_revision(rows, before, after, before_funnel, after_funnel):
    references = {}
    for row in rows:
        if row["status"] != "accepted":
            continue
        payload = json.loads(row["payload"])
        references[row["event_id"]] = {
            "event_id": row["event_id"],
            "receipt_id": row["id"],
            "event_type": payload["event_type"],
            "occurred_at": payload["occurred_at"],
            "received_at": row["received_at"],
            "source": row["source"],
        }
    before_cutoff = before["window"]["as_of"]
    after_cutoff = after["window"]["as_of"]
    before_excluded = {e["event_id"]: e["reason"] for e in before["exclusions"]}
    after_excluded = {e["event_id"]: e["reason"] for e in after["exclusions"]}
    old_rows = {entry["event_id"]: entry for entry in before["ledger"]}
    new_rows = {entry["event_id"]: entry for entry in after["ledger"]}

    def refs(ids):
        return [references[eid] for eid in dict.fromkeys(ids)]

    def enrich(entry):
        return dict(entry, source_refs=refs(entry["source_event_ids"])) if entry else None

    changes = []
    for eid in old_rows.keys() | new_rows.keys():
        old, new = old_rows.get(eid), new_rows.get(eid)
        if old == new:
            continue
        old_sources = set(old["source_event_ids"]) if old else set()
        new_sources = set(new["source_event_ids"]) if new else set()
        before_known = references[eid]["received_at"] <= before_cutoff
        after_known = references[eid]["received_at"] <= after_cutoff
        old_money, new_money = _money(old), _money(new)
        if new and not old and not before_known and after_known:
            reason = "new_delivery"
        elif new and not old and before_known and before_excluded.get(eid) == "missing_predecessor":
            reason = "chain_became_valid"
        elif old and new and old["payment_event_id"] != new["payment_event_id"]:
            reason = "payment_relinked"
        elif old and new and old_money == new_money and old_sources != new_sources:
            reason = "provenance_changed"
        else:
            reason = "semantic_reclassification"
        changes.append(
            {
                "event_id": eid,
                "change": "changed" if old and new else ("added" if new else "removed"),
                "operation_type": "cancellation" if (new or old)["amount_cents"] < 0 else "payment",
                "before": enrich(old),
                "after": enrich(new),
                "contribution": {key: new_money[key] - old_money[key] for key in MONEY_FIELDS},
                "reason": reason,
                "evidence": {
                    "before_known": before_known,
                    "after_known": after_known,
                    "before_exclusion": before_excluded.get(eid),
                    "after_exclusion": after_excluded.get(eid),
                    "newly_known_source_event_ids": sorted(
                        e
                        for e in new_sources
                        if before_cutoff < references[e]["received_at"] <= after_cutoff
                    ),
                    "added_source_event_ids": sorted(new_sources - old_sources),
                    "removed_source_event_ids": sorted(old_sources - new_sources),
                },
            }
        )
    changes.sort(
        key=lambda change: (
            (change["after"] or change["before"])["occurred_at"],
            change["event_id"],
        )
    )
    totals = {key: sum(change["contribution"][key] for change in changes) for key in MONEY_FIELDS}
    funnel_changes = []
    for stage, old in before_funnel.items():
        new = after_funnel[stage]
        if old == new:
            continue
        old_sessions, new_sessions = set(old), set(new)
        funnel_changes.append(
            {
                "stage": stage,
                "before_sessions": len(old),
                "after_sessions": len(new),
                "delta_sessions": len(new) - len(old),
                "added_session_ids": sorted(new_sessions - old_sessions),
                "removed_session_ids": sorted(old_sessions - new_sessions),
                "changed_session_ids": sorted(
                    sid for sid in old_sessions & new_sessions if old[sid] != new[sid]
                ),
                "before_source_refs": refs(eid for sid in sorted(old) for eid in old[sid]),
                "after_source_refs": refs(eid for sid in sorted(new) for eid in new[sid]),
            }
        )
    return {"ledger_changes": changes, "ledger_delta": totals, "funnel_changes": funnel_changes}
