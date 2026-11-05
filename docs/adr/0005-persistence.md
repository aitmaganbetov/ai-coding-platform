# ADR-0005: Хранение данных — SQLAlchemy 2 + Alembic, SQLite / PostgreSQL

- Статус: Принято (реализация — S2)
- Дата: 2026-11-05

## Контекст
ТЗ §4/§13: SQLite для single-user, PostgreSQL для multi-user; артефакты на локальном диске или MinIO. Сущности: users, projects, repositories, runs, tasks, task_dependencies, agents, agent_executions, approvals, artifacts, findings, policies, model_usage, audit_events, checkpoints. UUID, время UTC, optimistic concurrency (version) для состояния задач. NFR-03: сбой исполнителя не теряет подтверждённые состояния.

## Решение
- SQLAlchemy 2.x (typed ORM, Core для горячих запросов) + Alembic; **одни миграции** для SQLite и PostgreSQL (без диалект-специфичных типов, JSON — через `JSON`/`JSONB` variant).
- Первичные ключи — UUIDv7 (сортируемые по времени), все `timestamp` — `timezone=True`, UTC.
- Таблицы состояния (`runs`, `tasks`) имеют колонку `version`; обновление — `UPDATE ... WHERE id=:id AND version=:v` (optimistic locking), конфликт → повтор чтения.
- `audit_events` — append-only (в PostgreSQL триггер запрещает UPDATE/DELETE; в SQLite — проверка в репозитории); поля `actor, project_id, run_id, timestamp, outcome` обязательны (ТЗ §3).
- SQLite: WAL-режим, `foreign_keys=ON`, `busy_timeout`; файл `.ai-coding/state.db`.
- Артефакты: порт `ArtifactStore` с адаптерами `LocalFsArtifactStore` и `MinioArtifactStore`; содержимое адресуется SHA-256 (дедупликация, проверка целостности).
- Доступ к данным — через репозитории (порты) в `persistence/`; прикладной код не импортирует SQLAlchemy.
- Удаление проекта и экспорт данных (NFR-08) — отдельные сценарии с approval и retention-политикой.

## Последствия
Local Lite не требует сервера БД. Тесты прогоняются на SQLite и (в CI, S8) на PostgreSQL через service container.

## Альтернативы
Raw SQL — отклонено (две диалекта, нет миграций). SQLModel — отклонено (меньше контроля над типами Core/JSON variant). Event sourcing целиком — избыточно; используется журнал событий только для runs/tasks (ADR-0006).
