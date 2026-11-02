"""Команды загрузки, воспроизведения и проверки аналитики."""

import argparse
import json
import os
import re
import sys
from pathlib import Path
from time import perf_counter

from sqlalchemy.exc import SQLAlchemyError

from productpulse.analytics import compare, report
from productpulse.exports import ledger_csv
from productpulse.simulator import generate, seed
from productpulse.snapshots import (
    SnapshotIntegrityError,
    SnapshotNotFound,
    export_snapshot,
    list_snapshots,
    save_snapshot,
)
from productpulse.store import Store


class RussianArgumentParser(argparse.ArgumentParser):
    """Локализация нашего интерфейса без изменения глобального gettext argparse."""

    def __init__(self, *args, **kwargs):
        kwargs["add_help"] = False
        super().__init__(*args, **kwargs)
        self._positionals.title = "Аргументы"
        self._optionals.title = "Параметры"
        self.add_argument("-h", "--help", action="help", help="Показать справку и завершить")

    def format_usage(self):
        return super().format_usage().replace("usage:", "Использование:", 1)

    def format_help(self):
        return super().format_help().replace("usage:", "Использование:", 1)

    def error(self, message):
        required = "the following arguments are required: "
        unknown = "unrecognized arguments: "
        number = re.fullmatch(r"argument (.+): invalid int value: (.+)", message)
        missing = re.fullmatch(r"argument (.+): expected one argument", message)
        choice = re.fullmatch(r"argument (.+): invalid choice: (.+) \(choose from (.+)\)", message)
        if message.startswith(required):
            translated = "Не указаны обязательные аргументы: " + message[len(required) :]
        elif message.startswith(unknown):
            translated = "Неизвестные аргументы: " + message[len(unknown) :]
        elif number:
            translated = f"Аргумент {number[1]}: требуется целое число; получено {number[2]}"
        elif missing:
            translated = f"Аргумент {missing[1]} требует значение"
        elif choice:
            translated = f"Недопустимое значение {choice[2]}; варианты: {choice[3]}"
        else:
            translated = "Не удалось разобрать аргументы. Проверьте формат и справку --help."
        self.print_usage(sys.stderr)
        self.exit(2, f"Ошибка: {translated}\n")


def parser():
    root = RussianArgumentParser(description="ProductPulse — проверяемая аналитика аренды")
    root.add_argument(
        "--database",
        default=os.environ.get("PRODUCTPULSE_DATABASE_URL", "sqlite:///productpulse.db"),
        help="URL базы; предпочтительно переменная PRODUCTPULSE_DATABASE_URL",
    )
    commands = root.add_subparsers(dest="command", required=True, title="Команды")
    commands.add_parser("init", help="Создать таблицы и защиту квитанций")
    ingest = commands.add_parser("ingest", help="Принять файл JSONL, сохранив каждую строку")
    ingest.add_argument("file", type=Path)
    ingest.add_argument("--source", required=True)
    ingest.add_argument("--received-at", help="Историческое время получения ISO 8601 с поясом")
    demo = commands.add_parser("demo", help="Загрузить синтетический сценарий и сверить итоги")
    demo.add_argument("--seed", type=int, default=27)
    demo.add_argument("--users", type=int, default=240)
    demo.add_argument("--weeks", type=int, default=8)
    demo.add_argument("--output", type=Path, help="Сохранить сценарий с контрольным журналом")
    for name, help_text in (
        ("report", "Рассчитать снимок JSON"),
        ("export", "Вывести журнал оплат и отмен CSV"),
        ("compare", "Сравнить два исторических среза"),
        ("snapshot-save", "Сохранить неизменяемые JSON и CSV отчёта"),
    ):
        sub = commands.add_parser(name, help=help_text)
        sub.add_argument("--start", required=True)
        sub.add_argument("--end", required=True, help="Исключительная граница UTC")
        sub.add_argument("--channel")
        sub.add_argument("--category")
        if name == "compare":
            sub.add_argument("--before", required=True)
            sub.add_argument("--after", required=True)
        else:
            sub.add_argument("--as-of")
    archives = commands.add_parser("snapshot-list", help="Показать сохранённые архивные снимки")
    archives.add_argument("--offset", type=int, default=0)
    archives.add_argument("--limit", type=int, default=20)
    archived_export = commands.add_parser(
        "snapshot-export", help="Экспортировать точные байты архива"
    )
    archived_export.add_argument("id", help="UUID архивного снимка")
    archived_export.add_argument("--format", choices=("json", "csv"), required=True)
    archived_export.add_argument(
        "--output", type=Path, required=True, help="Новый файл; перезапись запрещена"
    )
    serve = commands.add_parser("serve", help="Открыть локальную панель и API")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8010)
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    db = Store(args.database)
    try:
        db.initialize()
        if args.command == "init":
            output = {"status": "готово", "database": db.engine.dialect.name}
        elif args.command == "ingest":
            with args.file.open(encoding="utf-8-sig", newline="") as stream:
                output = db.ingest(
                    (line.rstrip("\r\n") for line in stream),
                    source=args.source,
                    received_at=args.received_at,
                )
        elif args.command == "demo":
            started = perf_counter()
            scenario = generate(seed_value=args.seed, users=args.users, weeks=args.weeks)
            output = seed(db, scenario)
            output.update(
                start=scenario["start"],
                end=scenario["end"],
                final_as_of=scenario["final_as_of"],
                elapsed_seconds=round(perf_counter() - started, 3),
                deliveries=len(scenario["deliveries"]),
            )
            if args.output:
                with args.output.open("x", encoding="utf-8") as file:
                    json.dump(scenario, file, ensure_ascii=False, indent=2)
            print(json.dumps(output, ensure_ascii=False, indent=2))
            return 0 if output["reconciled"] else 1
        elif args.command == "snapshot-list":
            output = list_snapshots(db, offset=args.offset, limit=args.limit)
        elif args.command == "snapshot-export":
            output = export_snapshot(db, args.id, format=args.format, output=args.output)
        elif args.command == "serve":
            import uvicorn

            from productpulse.api import create_app

            uvicorn.run(create_app(db), host=args.host, port=args.port)
            return 0
        else:
            filters = {key: getattr(args, key) for key in ("start", "end", "channel", "category")}
            if args.command == "snapshot-save":
                output = save_snapshot(db, as_of=args.as_of, **filters)
            elif args.command == "compare":
                output = compare(db, before=args.before, after=args.after, **filters)
            else:
                output = report(db, as_of=args.as_of, **filters)
                if args.command == "export":
                    print(ledger_csv(output), end="")
                    return 0
        print(json.dumps(output, ensure_ascii=False, indent=2))
        return 0
    except OSError as exc:
        print(
            f"Ошибка доступа к файлу: {exc.filename or 'проверьте путь и права'}", file=sys.stderr
        )
        return 2
    except (ValueError, SnapshotNotFound, SnapshotIntegrityError) as exc:
        print(f"Ошибка: {exc}", file=sys.stderr)
        return 2
    except SQLAlchemyError:
        print("Ошибка базы данных: проверьте подключение и права доступа.", file=sys.stderr)
        return 2
    finally:
        db.close()
