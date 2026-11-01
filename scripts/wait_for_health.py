"""Ограниченное ожидание HTTP-готовности для контейнерного CI, без зависимостей."""

import json
import sys
import time
from http.client import HTTPException
from urllib.error import URLError
from urllib.request import urlopen


def wait_for_health(url, *, timeout=60):
    deadline = time.monotonic() + timeout
    last_failure = "ответ ещё не получен"
    while True:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError(
                f"Сервис не готов за {timeout:g} с; последняя проверка: {last_failure}"
            )
        try:
            with urlopen(url, timeout=min(2, remaining)) as response:
                payload = json.load(response)
                if (
                    response.status == 200
                    and isinstance(payload, dict)
                    and payload.get("status") == "ok"
                ):
                    return payload
                last_failure = "ответ не подтверждает status=ok"
        except (URLError, ConnectionError, TimeoutError, HTTPException) as exc:
            last_failure = f"HTTP {exc.code}" if hasattr(exc, "code") else type(exc).__name__
        except (json.JSONDecodeError, UnicodeDecodeError):
            last_failure = "некорректный JSON ответа"
        time.sleep(min(0.2, max(0, deadline - time.monotonic())))


if __name__ == "__main__":
    try:
        print(json.dumps(wait_for_health("http://127.0.0.1:8010/api/health")))
    except TimeoutError as exc:
        print(f"Ошибка готовности: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
