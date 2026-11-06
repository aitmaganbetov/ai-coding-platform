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
| [0025](0025-develop-loop-sandbox-tests-review.md) | DEVELOP: цикл реализация→тесты→review, тесты в Docker-песочнице, рекомендательный review | Принято | S6 |
| [0026](0026-modernize-and-rollback.md) | MODERNIZE: baseline, план шагов, малые PR, откат через git revert | Принято | S6 |
| [0027](0027-project-tools-and-offline-data.md) | Инструменты стеков в песочнице, офлайн-данные в кэше платформы, подавления, воспроизводимость | Принято | S7 |
| [0028](0028-s7-agents-and-advisory-triage.md) | Агенты Security, QA, Performance, DevOps, Maintenance; рекомендательный triage; prompt injection | Принято | S7 |
| [0029](0029-maintain-and-dependency-upgrades.md) | MAINTAIN: утверждённый план, обновление зависимостей пакетным менеджером, регрессионные тесты | Принято | S7 |
| [0030](0030-server-api-rbac-queue.md) | Self-Hosted сервер: API, аутентификация, RBAC, изоляция проектов, idempotency, очередь в БД и worker'ы | Принято | S8 |
| [0031](0031-server-deployment-storage-ui.md) | Развёртывание (образ, compose, TLS, backup, ротация), S3/MinIO, Web UI, метрики, экспорт и retention | Принято | S8 |
| [0032](0032-acceptance-load-and-security-testing.md) | Приёмочная матрица AC-01…AC-10, нагрузочные (k6) и security-тесты перед релизом | Принято | S9 |
| [0033](0033-sandbox-egress-and-e2e-tools.md) | Egress-allowlist песочницы (прокси, internal-сеть, защита от SSRF), Playwright и k6 для проектов | Принято | S9 |
| [0034](0034-server-scaling-and-isolation.md) | Масштабирование и изоляция сервера: процессы API, общий rate limiter, пул БД, rootless или удалённый Docker, S3 | Принято | S9 |
| [0035](0035-runtime-isolation-and-release.md) | Изоляция выполнения (gVisor, Job Objects Windows) и процесс релиза (SBOM, Sigstore, SLSA) | Принято | S9 |
| [0036](0036-workspaces-and-browser-terminal.md) | Рабочие области и терминал в браузере: Claude Code и Codex на подписках пользователей, изоляция, git только в контейнере | Принято | — |
| [0037](0037-ai-chat-agents.md) | Чат с ИИ в рабочей области: Claude Code (stream-json) и Codex (app-server) в фоне, разрешения и вход как в VS Code | Принято | — |
| [0048](0048-deploy-adapter.md) | Адаптер деплоя: среды (GitHub Actions, подписанный webhook), run `deploy` с Quality Gates, подтверждением Production и проверкой здоровья; только публичные https-адреса | Принято | — |
| [0047](0047-local-cli-reports.md) | Local (CLI) на сервере: `ai-coding server login`/`link`, сводки локальных прогонов (без кода и находок) в `local_runs`, карточка «Инфраструктуры» | Принято | — |
| [0046](0046-ai-providers-from-web.md) | ИИ-провайдер оркестратора из веба: раздел `ai` настроек проекта поверх `.ai-coding.yml`, ключ — секрет `ai.api_key` через реестр учётных данных (не в окружении), Private Mode, проверка подключения | Принято | — |
| [0045](0045-split-view-and-workspace-tabs.md) | Вкладки редактора и Split View (файл открыт в одной группе, перенос вкладки с несохранёнными правками), переход к строке по ссылке, вкладки рабочих областей, режим «Во весь экран» | Принято | — |
| [0044](0044-junit-test-results.md) | Результаты отдельных тестов: отчёты JUnit (pytest, `node --test`, Vitest; go test и cargo test переводятся) как артефакты, безопасный разбор, `GET /runs/{id}/tests`, регрессии и «Тестовые прогоны» | Принято | — |
| [0043](0043-github-actions.md) | GitHub Actions со стороны сервера: подключение и переключатель во вкладке «Интеграции», проверка подключения, запуски и dispatch через сервер, сводка в «Инфраструктуре» | Принято | — |
| [0042](0042-chat-tabs-and-plan-limits.md) | Вкладки чатов, удаление чата и лимиты подписок: служба чата следит за всеми открытыми чатами клиента, `delete`, лимиты из `/usage` Claude Code и rate limits Codex | Принято | — |
| [0041](0041-ai-in-the-editor.md) | ИИ в редакторе: изменения агента с «Принять»/«Отклонить» прямо в коде, ИИ-действия над выделением через чат рабочей области | Принято | — |
| [0040](0040-ai-changes-review.md) | AI Changes Review: изменения рабочей области по фрагментам — принять (индекс), отклонить (откат), коммит принятого; git в контейнере пользователя | Принято | — |
| [0039](0039-nexus-web-ui.md) | Web UI «NEXUS Developer OS»: дизайн-токены и три темы, App Shell, командная палитра, разделы на реальных данных | Принято | — |
| [0038](0038-orchestrator-tools-for-chat-agents.md) | Оркестратор как инструменты агентов чата: MCP-сервер `platform`, почтовый ящик рабочей области, аудит снимка ветки | Принято | — |
