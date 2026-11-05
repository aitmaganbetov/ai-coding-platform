# Architecture Decision Records

Формат — [MADR-lite](0001-record-architecture-decisions.md): *Контекст → Решение → Последствия → Альтернативы*.
Любое изменение требований ТЗ оформляется новым ADR или Change Request (ТЗ, конец документа).

| ADR | Решение | Статус | Спринт |
| --- | --- | --- | --- |
| [0001](0001-record-architecture-decisions.md) | Фиксировать архитектурные решения в ADR | Принято | S1 |
| [0002](0002-python-toolchain-and-packaging.md) | Python 3.12+, src-layout, hatchling, uv + lockfile | Принято | S1 |
| [0003](0003-modular-monolith-ports-and-adapters.md) | Модульный монолит, порты и адаптеры, слои под контролем import-linter | Принято | S1 |
| [0004](0004-configuration-schema.md) | `.ai-coding.yml`: Pydantic — источник истины, JSON Schema 2020-12 генерируется | Принято | S1 |
| [0005](0005-persistence.md) | SQLAlchemy 2 + Alembic; SQLite (Local) / PostgreSQL (Self-Hosted) | Принято | S1 (реализация S2) |
| [0006](0006-orchestrator-fsm-dag-checkpoints.md) | Оркестратор: явный FSM + DAG + журнал событий + checkpoints | Принято | S1 (реализация S2) |
| [0007](0007-execution-engine.md) | Единый интерфейс Executor; Local / Docker / GitHub Actions; deny-by-default | Принято | S1 (реализация S3–S4) |
| [0008](0008-git-abstraction.md) | Git через CLI-адаптер, изоляция в worktree, защита веток | Принято | S1 |
| [0009](0009-llm-providers-optional-ai.md) | Адаптеры LLM, AI опционален, бюджеты и маршрутизация | Принято | S1 (реализация S2/S5) |
| [0010](0010-security-model.md) | Модель угроз: недоверенный контент, least privilege, redaction, egress | Принято | S1 |
| [0011](0011-agent-contracts.md) | Единый контракт агентов, валидация JSON Schema | Принято | S1 |
| [0012](0012-quality-gates.md) | Детерминированные Quality Gates PASS/FAIL/SKIPPED, независимые от AI | Принято | S1 (реализация S3/S7) |
| [0013](0013-queue-and-worker.md) | Очередь: БД-очередь по умолчанию, Redis опционально | Принято | S1 (реализация S8) |
| [0014](0014-api-and-web-ui.md) | FastAPI + OpenAPI; Web UI на React + TypeScript + Vite | Принято | S1 (реализация S8) |
| [0015](0015-github-actions-integration.md) | GitHub Actions: reusable workflows, SHA-pinning, минимальные permissions | Принято | S1 (реализация S4) |
