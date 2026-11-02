# Воспроизводимый путь: поздняя доставка → ревизия → архив

Этот сценарий проверен на чистой установке коммита `2ce627b`: одна аренда на 100 USD, затем полная отмена. Команды ниже — для PowerShell, Python 3.12, из корня **новой копии репозитория**. Нужны свободный порт 8123 и отсутствующие файлы `walkthrough.db`, `cancel.jsonl`, `frozen-*.json`, `frozen-*.csv`. Старую базу не удаляйте: для повтора возьмите новую копию. Здесь не запускается большое демо.

## 1. Установить пакет

```powershell
python -m venv .venv
& .venv\Scripts\python.exe -m pip install -c constraints.txt .
$env:PATH = (Resolve-Path .venv\Scripts).Path + ';' + $env:PATH
```

Активация скриптом не требуется; PATH меняется только для текущего окна. При установке создаётся wheel, код берётся из установленного пакета. Для разработки и PostgreSQL есть отдельные команды в [README](../README.md).

## 2. Доставить оплату, затем её запоздавшую цепочку

```powershell
productpulse --database sqlite:///walkthrough.db ingest examples/late-payment.jsonl --source checkout --received-at 2026-01-06T00:00:00Z
productpulse --database sqlite:///walkthrough.db report --start 2026-01-01 --end 2026-02-01 --as-of 2026-01-07T00:00:00Z
productpulse --database sqlite:///walkthrough.db ingest examples/late-predecessors.jsonl --source delayed-sync --received-at 2026-01-08T00:00:00Z
$revision = productpulse --database sqlite:///walkthrough.db compare --start 2026-01-01 --end 2026-02-01 --before 2026-01-07T00:00:00Z --after 2026-01-09T00:00:00Z | ConvertFrom-Json
$revision.revision.ledger_delta
$revision.revision.ledger_changes | Select-Object event_id,change,reason
```

Первая загрузка: `accepted=1`, ранний `net_cents=0`. Вторая: `accepted=3`. Ревизия добавляет `late-paid`, причина `chain_became_valid`; вклады `gross_cents=10000`, `reversed_cents=0`, `net_cents=10000`. Оплата совершена 5 января и известна с 6-го; её предшественники совершены раньше оплаты, но получены 8-го. Все даты — UTC, правая граница периода не включается.

## 3. Сохранить точный результат

```powershell
$archive = productpulse --database sqlite:///walkthrough.db snapshot-save --start 2026-01-01 --end 2026-02-01 --as-of 2026-01-09T00:00:00Z | ConvertFrom-Json
productpulse --database sqlite:///walkthrough.db snapshot-export $archive.id --format json --output frozen-before.json
productpulse --database sqlite:///walkthrough.db snapshot-export $archive.id --format csv --output frozen-before.csv
```

UUID и время создания будут своими. В JSON `metrics.net_cents=10000`, CSV содержит оплату `+10000`. Эти два артефакта получены из одного захвата истории и записаны одной транзакцией.

## 4. Добавить историческую отмену и увидеть отличие живого отчёта

Создаём **синтетическую** полную отмену той же аренды. Поле суммы удаляется: величину полного возврата проектор берёт из оплаты.

```powershell
python -c "import json,pathlib; p=json.loads(pathlib.Path('examples/late-payment.jsonl').read_text()); p.update(event_id='late-cancelled',event_type='cancelled',occurred_at='2026-01-06T10:00:00Z'); p.pop('amount_cents'); pathlib.Path('cancel.jsonl').write_text(json.dumps(p)+'\n',encoding='utf-8')"
productpulse --database sqlite:///walkthrough.db ingest cancel.jsonl --source historical-cancel --received-at 2026-01-08T12:00:00Z
$live = productpulse --database sqlite:///walkthrough.db report --start 2026-01-01 --end 2026-02-01 --as-of 2026-01-09T00:00:00Z | ConvertFrom-Json
$live.metrics
productpulse --database sqlite:///walkthrough.db snapshot-export $archive.id --format json --output frozen-after.json
productpulse --database sqlite:///walkthrough.db snapshot-export $archive.id --format csv --output frozen-after.csv
```

Получено `accepted=1`. При **том же as_of** живой отчёт теперь показывает оплату 10000, отмену 10000, чистую стоимость 0 центов; архив по-прежнему 10000. Мы намеренно разрешили лабораторному импортёру добавить доставку задним числом. Поэтому as_of без неизменной истории сам по себе не фиксирует результат.

## 5. Сверить деньги и контрольные суммы

```powershell
(Get-Content frozen-after.json -Raw | ConvertFrom-Json).metrics.net_cents
(Import-Csv frozen-after.csv | Measure-Object amount_cents -Sum).Sum
(Get-FileHash frozen-before.json).Hash -eq (Get-FileHash frozen-after.json).Hash
(Get-FileHash frozen-before.csv).Hash -eq (Get-FileHash frozen-after.csv).Hash
(Get-FileHash frozen-after.json).Hash -eq $archive.report_sha256
(Get-FileHash frozen-after.csv).Hash -eq $archive.csv_sha256
```

Результат: `10000`, `10000`, затем четыре `True`. PowerShell сравнивает строковые хеши без учёта регистра. Экспорт использует новые имена: существующий файл он не перезаписывает.

## 6. Открыть интерфейс на этой же базе

```powershell
productpulse --database sqlite:///walkthrough.db serve --port 8123
```

Откройте [панель](http://127.0.0.1:8123/). Установите период 2026-01-01 — 2026-02-01, «Известно к моменту» 2026-01-09 00:00 UTC, все каналы и категории; нажмите «Применить». Живая чистая стоимость — 0 USD. Выберите сохранённый UUID и «Открыть архив»: 100 USD, явная архивная маркировка, фильтры и сравнение отключены. «К живому отчёту» вернёт 0 USD. Сервер останавливается Ctrl+C.

После шага 4 повторное живое сравнение 7 и 9 января уже включает отмену: grossΔ=10000, reversedΔ=10000, netΔ=0. Не ожидайте прежнюю ревизию +10000 из шага 2: она была рассчитана **до** нового импорта. Архив сохраняет отчёт, а не ответ `compare`.

## Границы и чтение кода

Данные синтетические; чистая стоимость бронирований не является бухгалтерской выручкой. Панель без авторизации предназначена для локального разбора. Проектор читает историю в память, нагрузочные измерения 10k/100k не выполнены. Архив хранит результат и ссылки, а не автономную копию raw-доставок. SHA-256 не цифровая подпись; администратор базы может отключить защитные триггеры. При сбое файлового экспорта возможен частичный новый файл. Подробности — [семантика](METRICS.md) и [фактические проверки](VERIFICATION.md).

| Вопрос для разбора | Точка входа |
|---|---|
| Как сохраняется каждая доставка и отвергается изменение исходника? | [store.py](../src/productpulse/store.py): `Store.ingest`, `initialize` |
| Почему неполная цепочка сначала не учитывается? | [analytics.py](../src/productpulse/analytics.py): `_project`, `report` |
| Откуда причина и точный денежный вклад ревизии? | [revisions.py](../src/productpulse/revisions.py): `explain_revision` |
| Почему архив не пересчитывается? | [snapshots.py](../src/productpulse/snapshots.py): `save_snapshot`, `read_snapshot_bytes` |
| Где проверена сохранность после новых доставок? | [test_snapshots.py](../tests/test_snapshots.py) |
| Как соединены команды, HTTP и режимы панели? | [cli.py](../src/productpulse/cli.py), [api.py](../src/productpulse/api.py), [app.js](../src/productpulse/static/app.js) |

---

## Разбор большого демо за пять минут

1. Запустите `productpulse --database sqlite:///demo.db demo`, затем `productpulse --database sqlite:///demo.db serve`. Откройте `http://127.0.0.1:8010/`. Данные целиком синтетические; внешних изображений, шрифтов и запросов к сервисам нет.
2. Начните с зелёной карточки чистой стоимости. В демо по умолчанию она равна 60 272 USD. Сумма оплат 68 684 USD минус 8 412 USD полных отмен. Это стоимость бронирований, не бухгалтерская выручка.
3. Сравните два момента знания. При неизменном периоде событий поздняя доставка может добавить оплату или отмену, либо восстановить ранее неполную цепочку. Три отдельные суммы показывают изменение оплат, отмен и чистой стоимости. В таблице каждая изменившаяся операция имеет точный вклад и доказательную причину. Кнопки «До» и «После» раскрывают канонические источники соответствующего среза; оттуда можно открыть исходную доставку. Разница включает все изменения знания между срезами, не только ошибки источников.
4. Отфильтруйте канал и категорию и нажмите «Применить». Экспорт заново рассчитывает данные по последним применённым фильтрам, а не по ещё не отправленным значениям формы. Новые доставки после отображения панели могут изменить результат CSV. После смены фильтров сравнение нужно выполнить снова.
5. Посмотрите путь до оплаты. Случайный `paid` без `quoted` и `reserved` не увеличивает показатель. Воронка начинается с просмотра в выбранном периоде, поэтому число завершённых путей может отличаться от числа оплат периода.
6. В удержании `—` означает ещё не наблюдавшуюся неделю. Пунктир и `◌` обозначают неполную календарную неделю. Полная неделя тоже может пересчитаться при доставке новых данных: зрелость календаря не равна полноте источника.
7. Нажмите «Конфликт» или «Невалидно» в блоке качества. Квитанции можно листать и раскрывать до исходного JSON, причины и SHA-256. Счётчики качества относятся ко всей базе до выбранного времени знания, а не только к сегменту.
8. В журнале операций откройте кнопку «4 события» у оплаты или «5 событий» у отмены. Вы увидите весь причинный путь, время события и время получения каждой доставки. Отрицательная строка отмены ссылается на исходный платёж.
9. Скачайте CSV. Сумма `amount_cents` равна `net_cents` заново рассчитанного при экспорте отчёта. Заголовок `X-Snapshot-Fingerprint` относится к этому экспортированному отчёту. Текстовые поля защищены от запуска формул при открытии в табличном редакторе.
