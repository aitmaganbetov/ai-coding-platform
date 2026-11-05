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

> **Статус: Sprints S1–S2 завершены.** S1 — архитектура, ADR, схема конфигурации, CLI, Git-абстракция, контракт агентов, CI. S2 — оркестратор (конечный автомат, DAG, checkpoints, retry, политики, approvals, бюджеты, аудит), хранилище SQLite + Alembic, восстановление после падения worker'а без дублирования коммитов, LLM-порт с guard'ом, команды `ai-coding run …`. Исполнители, агенты и API — S3–S9, см. [backlog](docs/backlog.md).

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
uv run ai-coding run discover -C /path/to/project
```

Подробнее: [установка](docs/installation.md) · [конфигурация](docs/configuration.md) · [CLI](docs/cli.md).

## Документация

- [Архитектура](docs/architecture/overview.md) и [модули/зависимости](docs/architecture/modules.md)
- [ADR](docs/adr/README.md) — архитектурные решения
- [Оркестратор](docs/orchestrator.md) — компоненты, жизненный цикл run, как писать обработчики
- [Разработка](docs/development.md) — окружение, правила, тесты
- [Backlog](docs/backlog.md) — все требования ТЗ по спринтам
- [Замечания к ТЗ](docs/tz-notes.md) — сверка PDF и `.md`, интерпретации
- Отчёты: [S1](docs/sprints/S1-report.md), [S2](docs/sprints/S2-report.md)
- [Политика безопасности](SECURITY.md)
- Техническое задание: [AI_Coding_Platform_v2_1_TZ.pdf](AI_Coding_Platform_v2_1_TZ.pdf) (основной документ) · [Markdown-версия](AI_Coding_Platform_v2_1_TZ.md), сгенерированная из PDF

## Технологии

Python 3.12+ · Typer · Pydantic v2 · JSON Schema 2020-12 · SQLAlchemy 2 + Alembic · structlog · FastAPI (S8) · React + TypeScript (S8) · SQLite / PostgreSQL (S2/S8) · Redis (опц.) · Docker · GitHub Actions · OpenAI / Anthropic / Ollama / vLLM (S5).

## Лицензия

GPL-3.0-or-later — см. [LICENSE](LICENSE).
