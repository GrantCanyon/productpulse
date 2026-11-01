"""Проверка входных событий без исправления исходных данных."""

import json
import re
from datetime import UTC, datetime

KINDS = ("viewed", "quoted", "reserved", "paid", "cancelled")
FIELDS = {
    "event_id",
    "event_type",
    "occurred_at",
    "user_id",
    "session_id",
    "rental_id",
    "channel",
    "category",
    "amount_cents",
}


def timestamp(value: str, *, date_allowed=False) -> datetime:
    if not isinstance(value, str):
        raise ValueError("Время должно быть строкой ISO 8601")
    if date_allowed and re.fullmatch(r"\d{4}-\d{2}-\d{2}", value):
        return datetime.fromisoformat(value).replace(tzinfo=UTC)
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValueError("Некорректное время ISO 8601") from exc
    if result.tzinfo is None:
        raise ValueError("У времени должен быть часовой пояс")
    try:
        return result.astimezone(UTC)
    except OverflowError as exc:
        raise ValueError("Время после перевода в UTC выходит за допустимый диапазон") from exc


def iso(value: datetime) -> str:
    return value.astimezone(UTC).isoformat(timespec="microseconds").replace("+00:00", "Z")


def canonical(value) -> str:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    )


def validate(raw: str) -> dict:
    def unique(pairs):
        value = {}
        for key, item in pairs:
            if key in value:
                raise ValueError("Повторяющийся ключ JSON")
            value[key] = item
        return value

    try:
        value = json.loads(
            raw,
            object_pairs_hook=unique,
            parse_constant=lambda _: (_ for _ in ()).throw(
                ValueError("Недопустимое числовое значение")
            ),
        )
    except (json.JSONDecodeError, TypeError) as exc:
        raise ValueError("Некорректный JSON") from exc
    if not isinstance(value, dict):
        raise ValueError("Событие должно быть объектом JSON")
    if set(value) - FIELDS:
        raise ValueError("Неизвестные поля события")
    for field in FIELDS - {"amount_cents"}:
        if not isinstance(value.get(field), str) or not value[field].strip():
            raise ValueError(f"Поле {field}: обязательная непустая строка")
        if any(0xD800 <= ord(char) <= 0xDFFF or ord(char) == 0 for char in value[field]):
            raise ValueError(f"Поле {field}: недопустимый символ Unicode")
        if len(value[field]) > 200:
            raise ValueError(f"Поле {field}: не более 200 символов")
    if value["event_type"] not in KINDS:
        raise ValueError("Неизвестный тип события")
    value["occurred_at"] = iso(timestamp(value["occurred_at"]))
    amount = value.get("amount_cents")
    if value["event_type"] == "paid":
        if type(amount) is not int or not 0 < amount <= 1_000_000_000:
            raise ValueError("Платёж: amount_cents должен быть целым от 1 до 1000000000")
    elif "amount_cents" in value:
        raise ValueError("amount_cents допустим только для paid; отмена всегда полная")
    return value
