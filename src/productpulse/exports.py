"""Экспорт журнала оплат и отмен с защитой текстовых ячеек CSV."""

import csv
import io

FIELDS = [
    "event_id",
    "occurred_at",
    "user_id",
    "session_id",
    "rental_id",
    "channel",
    "category",
    "amount_cents",
    "payment_event_id",
    "source_event_ids",
]


def ledger_csv(snapshot):
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=FIELDS, extrasaction="ignore")
    writer.writeheader()
    for entry in snapshot["ledger"]:
        row = dict(entry, source_event_ids=" | ".join(entry["source_event_ids"]))
        for key, value in row.items():
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
                row[key] = "'" + value
        writer.writerow(row)
    return output.getvalue()
