# Architecture Decision Records

Формат — [MADR-lite](0001-record-architecture-decisions.md): *Контекст → Решение → Последствия → Альтернативы*.
Любое изменение требований ТЗ оформляется новым ADR или Change Request (ТЗ, конец документа).

| ADR | Решение | Статус | Спринт |
| --- | --- | --- | --- |
| [0001](0001-record-architecture-decisions.md) | Фиксировать архитектурные решения в ADR | Принято | S1 |
| [0002](0002-python-toolchain-and-packaging.md) | Python 3.12+, src-layout, hatchling, uv + lockfile | Принято | S1 |
| [0003](0003-modular-monolith-ports-and-adapters.md) | Модульный монолит, порты и адаптеры, слои под контролем import-linter | Принято | S1 |
| [0004](0004-configuration-schema.md) | `.ai-coding.yml`: Pydantic — источник истины, JSON Schema 2020-12 генерируется | Принято | S1 |
| [0005](0005-persistence.md) | SQLAlchemy 2 + Alembic; SQLite (Local) / PostgreSQL (Self-Hosted) | Принято | реализовано в S2 (PostgreSQL-профиль — S8) |
| [0006](0006-orchestrator-fsm-dag-checkpoints.md) | Оркестратор: явный FSM + DAG + журнал событий + checkpoints | Принято | реализовано в S2 |
| [0007](0007-execution-engine.md) | Единый интерфейс Executor; Local / Docker / GitHub Actions; deny-by-default | Принято | Local/Docker — S3 (ADR-0018); GitHub Actions — S4 |
| [0008](0008-git-abstraction.md) | Git через CLI-адаптер, изоляция в worktree, защита веток | Принято | S1 |
| [0009](0009-llm-providers-optional-ai.md) | Адаптеры LLM, AI опционален, бюджеты и маршрутизация | Принято | порт, guard, бюджеты — S2; адаптеры — S5 (ADR-0023) |
| [0010](0010-security-model.md) | Модель угроз: недоверенный контент, least privilege, redaction, egress | Принято | S1 |
| [0011](0011-agent-contracts.md) | Единый контракт агентов, валидация JSON Schema | Принято | S1; агенты CREATE — S5 (ADR-0024) |
| [0012](0012-quality-gates.md) | Детерминированные Quality Gates PASS/FAIL/SKIPPED, независимые от AI | Принято | движок — S3 (ADR-0020); адаптеры стеков — S7 |
| [0013](0013-queue-and-worker.md) | Очередь: БД-очередь по умолчанию, Redis опционально | Принято | S1 (реализация S8) |
| [0014](0014-api-and-web-ui.md) | FastAPI + OpenAPI; Web UI на React + TypeScript + Vite | Принято | S1 (реализация S8) |
| [0015](0015-github-actions-integration.md) | GitHub Actions: reusable workflows, SHA-pinning, минимальные permissions | Принято | S1; реализация — S4 (ADR-0022) |
| [0016](0016-worker-leases-and-effect-ledger.md) | Lease прогона, журнал эффектов, кооперативная отмена (AC-04) | Принято | S2 |
| [0017](0017-structured-logging.md) | structlog JSON + redaction + run_id/trace_id | Принято | S2 |
| [0018](0018-executors-implementation.md) | Исполнители: Docker CLI, `--pull never`, ограничения хоста | Принято | S3 |
| [0019](0019-tool-adapters-and-findings.md) | Адаптеры инструментов, fingerprint находок, офлайн-правила Semgrep, fail-closed | Принято | S3 |
| [0020](0020-quality-gates-semantics-and-baseline.md) | Семантика PASS/FAIL/SKIPPED, strict, baseline с владельцем и сроком | Принято | S3 |
| [0021](0021-github-integration.md) | GitHub: httpx-клиент, порт RepositoryHost, PR без merge, checks, Actions executor | Принято | S4 |
| [0022](0022-reusable-workflows.md) | Reusable workflows: права, форки, установка платформы, SHA-pinning, пилоты | Принято | S4 |
| [0023](0023-llm-adapters-routing-cache.md) | LLM-адаптеры на httpx, маршрутизация с fallback, кэш ответов | Принято | S5 |
| [0024](0024-agents-and-create-mode.md) | Агенты CREATE (порт, цикл исправления, детерминированный bootstrap) и сценарий CREATE | Принято | S5 |
