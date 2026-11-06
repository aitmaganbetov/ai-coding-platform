# AI Coding Platform v2.1

**Local, Self-Hosted & GitHub Hybrid** платформа управления полным жизненным циклом ПО с помощью AI-агентов: от идеи и ТЗ до разработки, аудита, модернизации, выпуска и сопровождения.

| Режим | Назначение |
| --- | --- |
| CREATE | Новый проект: требования → архитектура → план → каркас → MVP |
| DEVELOP | Новые функции и исправления в отдельной ветке/PR с тестами |
| MODERNIZE | Рефакторинг, обновление зависимостей, миграции с откатом |
| AUDIT | Технический, архитектурный, security и performance аудит (read-only) |
| MAINTAIN | Плановое сопровождение и устранение регрессий |

Ключевые принципы: GitHub и LLM **опциональны**; детерминированные Quality Gates (PASS/FAIL/SKIPPED) не зависят от AI; изменения только в отдельных git worktree/ветках; секреты никогда не попадают в конфигурацию, логи и prompts; опасные операции — только с human approval.

> **Статус: v2.1.0 — Sprints S1–S9 завершены, все приёмочные сценарии AC-01…AC-10 проходят в CI ([acceptance.md](docs/acceptance.md)).** S1 — архитектура, схема конфигурации, CLI, Git. S2 — оркестратор (конечный автомат, DAG, checkpoints, retry, политики, approvals, аудит), восстановление после падения worker'а. S3 — исполнители Local и Docker-песочница, реестр инструментов (Gitleaks, Semgrep, Ruff), Quality Gates и baseline, **`ai-coding audit`** — безопасный офлайн read-only аудит. S4 — GitHub: PR под политикой и approval (без merge), check runs и PR-комментарии без дублей, отчёты SARIF/JUnit, GitHub Actions executor, reusable workflows и пилоты на двух стеках ([github.md](docs/github.md)). S5 — LLM-адаптеры (OpenAI, Anthropic, Ollama, vLLM) с маршрутизацией, fallback и кэшем; агенты Requirements, Architect, Planner, Bootstrap; **`ai-coding create`** — новый проект из описания: согласованная версия ТЗ, ADR, backlog, каркас и PR ([create.md](docs/create.md)). S6 — **`ai-coding develop`** (issue → изменения с тестами → тесты в Docker-песочнице → AI review → ветка → PR), **`ai-coding modernize`** (baseline, план шагов, малые PR, откат), **`ai-coding review`** и reusable `ai-review.yml`; агенты Developer, Reviewer, Database ([develop.md](docs/develop.md)). S7 — полный **`ai-coding audit`** для стеков (Trivy, OSV-Scanner, PHPStan, Pint, ESLint, tsc, mypy, go test, golangci-lint, cargo test, Clippy) с офлайн-БД, воспроизводимостью и рекомендательным triage; **`ai-coding maintain`** (план → approval → обновление зависимостей пакетным менеджером → исправление с регрессионным тестом → PR); все 12 агентов; тесты prompt injection (AC-07) ([audit.md](docs/audit.md), [maintain.md](docs/maintain.md)). S8 — Self-Hosted сервер: REST API ТЗ §13 с `Idempotency-Key`, пользователи и токены, RBAC на сервере и изоляция проектов (AC-08), очередь в БД (PostgreSQL `SKIP LOCKED`) и worker'ы на ≥10 прогонов, Web UI на React, метрики Prometheus, хранилище артефактов S3/MinIO, docker-compose с TLS, backup, экспорт и retention ([server.md](docs/server.md)). S9 — hardening и релиз: приёмочная матрица AC-01…AC-10 в CI; нагрузочный тест k6; security-тесты (права по всем маршрутам, fuzzing API и конфигурации, выход из песочницы) и найденные ими исправления; egress-allowlist песочницы; gVisor; Playwright и k6 для проектов; SBOM, Sigstore, SLSA; [эксплуатационная инструкция](docs/operations.md) и [runbook](docs/runbook.md). Изменения — [CHANGELOG](CHANGELOG.md).

## Быстрый старт

```bash
uv sync --frozen
```

```bash
uv run ai-coding init /path/to/project --dry-run
```

```bash
uv run ai-coding init /path/to/project
```

```bash
uv run ai-coding config validate /path/to/project
```

```bash
uv run ai-coding tools pull
```

```bash
uv run ai-coding audit -C /path/to/project --backend docker -o reports/
```

Подробнее: [установка](docs/installation.md) · [конфигурация](docs/configuration.md) · [CLI](docs/cli.md).

## Документация

- [AI Security Center](docs/security-center.md) — выключенная по умолчанию основа модуля и поэтапный план внедрения
- [Архитектура](docs/architecture/overview.md) и [модули/зависимости](docs/architecture/modules.md)
- [ADR](docs/adr/README.md) — архитектурные решения
- [Аудит](docs/audit.md) — `ai-coding audit`, инструменты стеков, гейты, baseline, triage
- [Сопровождение](docs/maintain.md) — `ai-coding maintain`, обновление зависимостей
- [CREATE](docs/create.md) · [DEVELOP и MODERNIZE](docs/develop.md) · [GitHub](docs/github.md)
- [Self-Hosted сервер](docs/server.md) — API, роли, worker'ы, Web UI, развёртывание, backup, ротация ключей
- [Эксплуатация](docs/operations.md) и [runbook восстановления](docs/runbook.md) · [Приёмочные сценарии](docs/acceptance.md) · [CHANGELOG](CHANGELOG.md)
- [Оркестратор](docs/orchestrator.md) — компоненты, жизненный цикл run, как писать обработчики
- [Разработка](docs/development.md) — окружение, правила, тесты
- [Backlog](docs/backlog.md) — все требования ТЗ по спринтам
- [Замечания к ТЗ](docs/tz-notes.md) — сверка PDF и `.md`, интерпретации
- Отчёты: [S1](docs/sprints/S1-report.md), [S2](docs/sprints/S2-report.md), [S3](docs/sprints/S3-report.md), [S4](docs/sprints/S4-report.md), [S5](docs/sprints/S5-report.md), [S6](docs/sprints/S6-report.md), [S7](docs/sprints/S7-report.md), [S8](docs/sprints/S8-report.md), [S9](docs/sprints/S9-report.md)
- [Политика безопасности](SECURITY.md)
- Техническое задание: [AI_Coding_Platform_v2_1_TZ.pdf](AI_Coding_Platform_v2_1_TZ.pdf) (основной документ) · [Markdown-версия](AI_Coding_Platform_v2_1_TZ.md), сгенерированная из PDF

## Технологии

Python 3.12+ · Typer · Pydantic v2 · JSON Schema 2020-12 · SQLAlchemy 2 + Alembic · structlog · FastAPI · React + TypeScript · Vite · SQLite / PostgreSQL (S2/S8) · Redis (опц.) · Docker · GitHub Actions · OpenAI / Anthropic / Ollama / vLLM (S5).

## Лицензия

GPL-3.0-or-later — см. [LICENSE](LICENSE).
