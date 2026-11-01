-- PostgreSQL: диагностические запросы, только чтение.
-- Они проверяют квитанции, но не заменяют упорядоченный проектор бизнес-метрик.

-- Классификация доставок в конкретный момент знания.
SELECT status, count(*) AS deliveries
FROM receipts
WHERE received_at <= '2026-03-09T00:00:00.000000Z'
GROUP BY status
ORDER BY status;

-- Инвариант: запрос не должен возвращать строк.
SELECT event_id, count(*) AS canonical_events
FROM receipts
WHERE status = 'accepted'
GROUP BY event_id
HAVING count(*) > 1;

-- Все версии одной доставки: конфликт не заменяет канонический оригинал.
SELECT id, event_id, status, received_at, source, raw_sha256, reason
FROM receipts
WHERE event_id = 'session-00001-paid'
ORDER BY id;

-- Доставки, пришедшие позже времени события более чем на день.
SELECT id, event_id, received_at,
       payload::jsonb ->> 'occurred_at' AS occurred_at
FROM receipts
WHERE status = 'accepted'
  AND received_at::timestamptz >
      (payload::jsonb ->> 'occurred_at')::timestamptz + interval '1 day'
ORDER BY received_at;

-- Принятые события с временем события позже момента доставки.
SELECT id, event_id, received_at,
       payload::jsonb ->> 'occurred_at' AS occurred_at
FROM receipts
WHERE status = 'accepted'
  AND (payload::jsonb ->> 'occurred_at')::timestamptz > received_at::timestamptz;

-- Размеры таблицы и индексов в текущей базе.
SELECT pg_size_pretty(pg_total_relation_size('receipts')) AS total_size,
       pg_size_pretty(pg_relation_size('receipts')) AS table_size;
