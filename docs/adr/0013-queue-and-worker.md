# ADR-0013: Очередь задач и worker

- Статус: Принято (реализация — S2 локальный scheduler, S8 серверный worker)
- Дата: 2026-11-05

## Контекст
Redis — опционально (ТЗ §4). Offline-режим — через локальный scheduler (ТЗ §15). NFR-02: ≥10 параллельных заданий на сервере.

## Решение
- Порт `TaskQueue`: `enqueue`, `lease(worker_id, ttl)`, `heartbeat`, `ack`, `nack(retry_at)`.
- По умолчанию — **очередь в БД** (`SELECT ... FOR UPDATE SKIP LOCKED` в PostgreSQL; в SQLite — single-writer lease с `UPDATE ... WHERE lease_expires_at < now`). Не требует дополнительной инфраструктуры.
- `RedisTaskQueue` — опциональный адаптер (extra `redis`) для высокой нагрузки.
- Локальный режим: in-process scheduler (asyncio) поверх той же БД-очереди → одинаковая семантика восстановления.
- Lease с истечением: упавший worker теряет lease, задача возвращается в очередь и продолжает с checkpoint (AC-04).

## Альтернативы
Celery — отклонено (обязательный брокер, слабая интеграция с FSM/checkpoints). RQ — требует Redis всегда.
