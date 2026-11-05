# Backlog AI Coding Platform v2.1

Полный backlog по ТЗ v2.1. Каждый элемент трассируется к требованиям (FR/NFR/AC/P0, разделы ТЗ). Статусы: ✅ Done (S1, проверено тестами), 🔜 Next (S2), ⬜ Planned.

**Definition of Done** (ТЗ §19–20): код, тесты, обновлённая документация, безопасные значения по умолчанию, прохождение CI, проверенный сценарий отказа (если применимо), отчёт и одобрение ответственного.

Приоритеты: **P0** — блокирует Local MVP / ТЗ §19; **P1** — нужно для v2.1; **P2** — улучшение.

## Сводка по спринтам

| Спринт | Эпики | Приёмка (ТЗ §16) |
| --- | --- | --- |
| S1 | E01 Каркас, E02 Конфигурация, E03 Git-абстракция, E04 Контракты/схемы | `init` / `config validate`; тесты каркаса |
| S2 | E05 Persistence, E06 Orchestrator FSM, E07 Policy/Approval, E08 LLM-порт | Восстановление после прерывания; unit tests |
| S3 | E09 Executors Local/Docker, E10 Tool registry, E11 Quality Gates engine | Безопасный локальный audit |
| S4 | E12 GitHub adapter, E13 Reusable workflows, E14 Reports/PR | CI проходит на пилотном репозитории |
| S5 | E15 Agents I, E16 LLM adapters, E17 CREATE | Новый проект из задачи до PR |
| S6 | E18 Agents II, E19 DEVELOP, E20 MODERNIZE | Функция/рефакторинг через отдельный PR |
| S7 | E21 Agents III, E22 Security/quality tools, E23 AUDIT, E24 MAINTAIN | Воспроизводимые результаты сканеров |
| S8 | E25 API, E26 RBAC/Auth, E27 Worker/Queue, E28 Web UI, E29 Observability, E30 Deploy | Два проекта и несколько пользователей |
| S9 | E31 Hardening, E32 Load/Security tests, E33 Docs/Release | AC-01…AC-10, v2.1 |

---

## S1 — Архитектура, схемы, CLI, конфигурация, Git-абстракция

### E01 Каркас репозитория (P0-01)
| ID | Задача | Трассировка | Pri | Статус |
| --- | --- | --- | --- | --- |
| B-001 | Python-пакет `ai_coding` (src-layout, hatchling, py.typed) | §4, ADR-0002 | P0 | ✅ |
| B-002 | `pyproject.toml`, lockfile `uv.lock`, dependency group dev | NFR-05 | P0 | ✅ |
| B-003 | Кодстайл: ruff (lint+format), mypy strict, editorconfig, gitattributes | P0-01 | P0 | ✅ |
| B-004 | Контроль слоёв import-linter | ADR-0003 | P0 | ✅ |
| B-005 | CI: lint, mypy, слои, schema drift, тесты Linux/Windows/macOS × py3.12/3.13, coverage ≥80%, сборка wheel + smoke | P0-01, NFR-01, NFR-06 | P0 | ✅ |
| B-006 | CI безопасности: gitleaks (история, checksum-verified), pip-audit по lockfile, Dependabot | §9, §12 | P0 | ✅ |
| B-007 | Документация: архитектура, ADR, установка, разработка, конфигурация, CLI, backlog | §16 S1 | P0 | ✅ |
| B-008 | Branch protection/Rulesets для `main` в репозитории проекта (настройка GitHub, нужен админ) | §9, AC-03 | P0 | ⬜ (действие владельца) |

### E02 CLI и конфигурационная схема (P0-02)
| ID | Задача | Трассировка | Pri | Статус |
| --- | --- | --- | --- | --- |
| B-010 | `ai-coding --help`, `--version`, `python -m ai_coding` | §14 | P0 | ✅ |
| B-011 | Pydantic-модели `.ai-coding.yml` со всеми разделами ТЗ §11 | §11 | P0 | ✅ |
| B-012 | JSON Schema 2020-12, генерация + drift-тест; `ai-coding config schema` | §11, ADR-0004 | P0 | ✅ |
| B-013 | `ai-coding config validate` (text/json, коды выхода 0/1/2) | §11, §14 | P0 | ✅ |
| B-014 | Запрет секретов в YAML, безопасные сообщения об ошибках | §11, FR-18 | P0 | ✅ |
| B-015 | Безопасный YAML: SafeLoader, дубли ключей, лимит размера, UTF-8 | §12 | P0 | ✅ |
| B-016 | Межсекционные правила: local-only vs внешние LLM/GitHub, audit read-only, mandatory approvals, default branch protected | §12, FR-06, FR-17, AC-05 | P0 | ✅ |
| B-017 | `ai-coding init`: шаблон с комментариями, детекция стеков, `--dry-run`, `--force` с подтверждением и backup, атомарная запись | §14 | P0 | ✅ |
| B-018 | Определение стеков (Laravel/PHP, React/TS/JS, Python, Go, Rust), ручное переопределение | FR-02 | P0 | ✅ |
| B-019 | `ai-coding config migrate` между версиями схемы | §11 | P2 | ⬜ (нужен при первой новой версии) |
| B-020 | Иерархия конфигурации: пользовательский `~/.config/ai-coding/config.yml` (провайдеры, профили) + проектный | §11 | P1 | ⬜ S2 |

### E03 Git-абстракция (P0-04 частично)
| ID | Задача | Трассировка | Pri | Статус |
| --- | --- | --- | --- | --- |
| B-030 | Порт `GitRepository` и адаптер `LocalGitRepository` (status, branches, remote, commit) | §4 | P0 | ✅ |
| B-031 | Worktree add/list/remove, изоляция изменений | §18, AC-03 | P0 | ✅ |
| B-032 | Защищённые ветки (glob), запрет коммита в protected/detached | AC-03, FR-17 | P0 | ✅ |
| B-033 | Защита от исполнения кода из репозитория (fsmonitor, hooks), валидация ref-имён против option injection | §12 | P0 | ✅ |
| B-034 | Импорт: локальная папка (✅ open), Git URL clone в изолированный каталог с проверкой доступа | FR-01 | P0 | ⬜ S3 |
| B-035 | diff/patch API (apply, check, stat), rollback ветки | FR-05, §18 | P0 | ⬜ S3 |
| B-036 | Commit trailers `AI-Coding-Run/Task` для идемпотентного восстановления | AC-04 | P0 | ⬜ S2 |

### E04 Контракты и доменная модель
| ID | Задача | Трассировка | Pri | Статус |
| --- | --- | --- | --- | --- |
| B-040 | Перечисления ТЗ: режимы, состояния, роли, агенты, стеки, backend, провайдеры, severity, gate results, approvals | §2–§10 | P0 | ✅ |
| B-041 | Контракт агента `AgentTask`/`AgentResult`, проверка `output_schema`, отказ при невалидном выводе | §6, P0-07 | P0 | ✅ |
| B-042 | Redactor (токены OpenAI/Anthropic/GitHub/AWS/Slack/Google, JWT, PEM, URL-креды, присваивания) | §12, NFR-07 | P0 | ✅ |

---

## S2 — Orchestrator FSM, persistence, checkpoint, retry, policy (P0-03, P0-05)

### E05 Persistence
| ID | Задача | Трассировка | Pri |
| --- | --- | --- | --- |
| B-100 | SQLAlchemy-модели всех сущностей ТЗ §12 (users … checkpoints), UUIDv7, UTC | §12 | P0 |
| B-101 | Alembic, одни миграции для SQLite и PostgreSQL; тест миграций up/down | ADR-0005 | P0 |
| B-102 | Optimistic concurrency (`version`) для runs/tasks; тест конфликта | §12 | P0 |
| B-103 | Append-only `audit_events` (actor, project_id, run_id, timestamp, outcome) | §3, FR-16 | P0 |
| B-104 | `ArtifactStore` (локальный диск, SHA-256, retention) | §4, NFR-03, NFR-08 | P0 |
| B-105 | SQLite WAL/foreign keys/busy_timeout; каталог `.ai-coding/` | ADR-0005 | P0 |

### E06 Orchestrator Engine
| ID | Задача | Трассировка | Pri |
| --- | --- | --- | --- |
| B-110 | State Manager: таблица переходов, идемпотентные переходы, журнал; 100% ветвей тестами | §4, NFR-06 | P0 |
| B-111 | Task Planner: DAG, проверка циклов, критерии успеха, бюджет шага | §4 | P0 |
| B-112 | Workflow Executor: последовательное/параллельное исполнение, `max_parallel_tasks`, семафоры ресурсов | FR-08, NFR-02 | P0 |
| B-113 | Checkpoint Manager: атомарная фиксация после верификации шага | §4, NFR-03 | P0 |
| B-114 | Retry & Recovery: backoff+jitter, классификация ошибок, тайм-аут, отмена | FR-09 | P0 |
| B-115 | Восстановление после `kill -9` без дублирования коммитов (ключи эффектов) | AC-04 | P0 |
| B-116 | Agent Registry (встроенные + entry points) | FR-08 | P0 |
| B-117 | Agent Router: capability, права, стоимость, доступность | §4 | P0 |
| B-118 | Context Manager: индекс репозитория, минимальный контекст, исключение секретов/игнорируемых путей | §4, FR-18 | P0 |
| B-119 | Result Aggregator: итог со ссылками на evidence; AI не перекрывает статус теста | §4 | P0 |
| B-120 | Audit Logger + structlog JSON-логи с run_id и redaction | FR-16, NFR-07 | P0 |
| B-121 | CLI: `run status`, `run resume`, `run cancel` | §14 | P0 |

### E07 Policy & Approval (P0-05)
| ID | Задача | Трассировка | Pri |
| --- | --- | --- | --- |
| B-130 | Policy Engine: policy-as-code (allowlist команд, пути записи, egress, бюджеты), решения с обоснованием | FR-11, §12 | P0 |
| B-131 | Approval Manager: запросы, TTL, роль подтверждающего, CLI-подтверждение, `--require-approval` | §4, FR-17, AC-10 | P0 |
| B-132 | `--dry-run` для всех изменяющих операций | §14, AC-10 | P0 |
| B-133 | Критичные файлы (`approvals.critical_paths`) → approval | §4 | P0 |

### E08 LLM-порт и бюджеты
| ID | Задача | Трассировка | Pri |
| --- | --- | --- | --- |
| B-140 | Порт `LLMProvider`, структурированный вывод, подсчёт токенов/стоимости, `model_usage` | §4, FR-15 | P0 |
| B-141 | Бюджеты токенов/стоимости/времени на run, остановка при превышении | FR-15 | P0 |
| B-142 | Egress-guard + redaction перед каждым внешним вызовом | FR-18, §12 | P0 |
| B-143 | Fake-провайдер **только для тестов** (детерминированные ответы) | NFR-06 | P1 |

---

## S3 — Local/Docker executor, sandbox, tool registry (P0-04)

### E09 Executors
| ID | Задача | Трассировка | Pri |
| --- | --- | --- | --- |
| B-200 | Порт `Executor`, `CommandSpec`, `ExecutionResult`, контрактные тесты | ADR-0007 | P0 |
| B-201 | LocalExecutor: allowlist, approval `host_command`, таймаут, лимит вывода, rlimits/Job Objects, redaction вывода | §4, §12 | P0 |
| B-202 | DockerSandboxExecutor: `--network none`, `--read-only`, `--cap-drop ALL`, no-new-privileges, non-root, pids/cpu/memory, без Docker socket | §12 | P0 |
| B-203 | Образы инструментов с закреплёнными digest | NFR-05 | P0 |
| B-204 | Импорт Git URL: clone в sandbox, проверка доступа и состояния | FR-01 | P0 |
| B-205 | Egress allowlist в sandbox (прокси/сеть по политике) | §12 | P1 |

### E10 Tool registry
| ID | Задача | Трассировка | Pri |
| --- | --- | --- | --- |
| B-210 | Порт `ToolAdapter` (detect, version, command, parse), реестр, entry points | §4 | P0 |
| B-211 | Нормализованная модель находок (fingerprint, severity, file/line, evidence) | FR-06 | P0 |
| B-212 | Первые адаптеры для безопасного локального audit: Gitleaks, Semgrep, Ruff | §16 S3 | P0 |

### E11 Quality Gates engine (P0-08 частично)
| ID | Задача | Трассировка | Pri |
| --- | --- | --- | --- |
| B-220 | Гейты Build/Tests/Static/Security/Coverage/Review → PASS/FAIL/SKIPPED | §10, FR-11 | P0 |
| B-221 | Baseline с owner/expires_at/reason | §10 | P1 |
| B-222 | `ai-coding audit` (read-only, детерминированный, без LLM) — первая версия | FR-06 | P0 |

---

## S4 — GitHub adapter, reusable workflows, PR/report integration (P0-06, P0-08)

### E12 GitHub adapter
| ID | Задача | Трассировка | Pri |
| --- | --- | --- | --- |
| B-300 | Порт `RepositoryHost`; GitHub REST/GraphQL через httpx; GitHub App и fine-grained token | §4 | P0 |
| B-301 | Push ветки + создание PR, запрет push в protected, запрет auto-merge | FR-17, AC-03 | P0 |
| B-302 | Check runs / statuses; комментарии к PR с защитой от дублей (маркер + upsert) | FR-10 | P0 |
| B-303 | GitHubActionsExecutor (dispatch, ожидание, артефакты) | ADR-0007 | P0 |
| B-304 | Rulesets: проверка/рекомендация настроек защиты | §15 | P1 |

### E13 Reusable workflows
| ID | Задача | Трассировка | Pri |
| --- | --- | --- | --- |
| B-310 | `quality.yml`, `security.yml`, `test-php.yml`, `test-node.yml`, `test-python.yml`, `test-go.yml`, `test-rust.yml`, `ai-review.yml`, `release.yml` (`workflow_call`) | §9, FR-14 | P0 |
| B-311 | Минимальные permissions, SHA-pinning, environments+approval для секретов, fork PR read-only, без `pull_request_target` | §9 | P0 |
| B-312 | actionlint + zizmor в CI | §9 | P1 |
| B-313 | Пилотный репозиторий: CI проходит | §16 S4, AC-09 | P0 |

### E14 Reports
| ID | Задача | Трассировка | Pri |
| --- | --- | --- | --- |
| B-320 | Отчёты JSON, Markdown, SARIF 2.1.0, JUnit XML | FR-10, P0-08 | P0 |
| B-321 | `ai-coding report export` | §14 | P0 |

---

## S5 — CREATE: requirements, architect, planning, bootstrap (P0-07, P0-10)

### E15 Agents I
| ID | Задача | Трассировка | Pri |
| --- | --- | --- | --- |
| B-400 | Порт `Agent`, базовый класс: контракт, полномочия, схема вывода, ошибки, журналирование | §6 | P0 |
| B-401 | Requirements Agent → ТЗ, user stories, acceptance criteria | §6 | P0 |
| B-402 | Architect Agent → ADR, схема модулей, API/DB проект | §6 | P0 |
| B-403 | Planner Agent → backlog, зависимости, оценка | §6 | P0 |
| B-404 | Bootstrap Agent → каркас, CI, README, конфигурация | §6 | P0 |
| B-405 | JSON Schemas выходов агентов в `schemas/agents/` | §6 | P0 |
| B-406 | `ai-coding agents list` | §14 | P0 |

### E16 LLM adapters
| ID | Задача | Трассировка | Pri |
| --- | --- | --- | --- |
| B-410 | OpenAI adapter | §4 | P0 |
| B-411 | Anthropic adapter | §4 | P0 |
| B-412 | Ollama / vLLM adapters (OpenAI-совместимый API), offline | FR-12, AC-05 | P0 |
| B-413 | Маршрутизация по агенту и fallback-цепочки | FR-15 | P1 |
| B-414 | Кэширование ответов (по хэшу redacted-контекста) | §18 | P2 |

### E17 CREATE
| ID | Задача | Трассировка | Pri |
| --- | --- | --- | --- |
| B-420 | Диалог требований → версия ТЗ → approval → ADR → backlog → bootstrap → PR | FR-03, AC-01 | P0 |
| B-421 | `ai-coding create`, `ai-coding project add` | §14 | P0 |

---

## S6 — DEVELOP и MODERNIZE, AI review/fix, approvals (P0-09)

| ID | Задача | Трассировка | Pri |
| --- | --- | --- | --- |
| B-500 | Developer Agent → patch/commit, пояснение, тесты | §6 | P0 |
| B-501 | Reviewer Agent → замечания file/line/severity (рекомендательно) | §6, §10 | P0 |
| B-502 | DEVELOP: Issue → план → ветка → patch → тесты → review → PR | FR-04, AC-03 | P0 |
| B-503 | AI fix-loop с ограничением итераций и бюджета | FR-04 | P1 |
| B-504 | MODERNIZE: baseline, impact analysis, малые PR, миграционные планы, rollback | FR-05 | P0 |
| B-505 | Database Agent → анализ миграций, индексов, транзакций | §6 | P1 |
| B-506 | `ai-coding develop`, `ai-coding modernize` | §14 | P0 |

## S7 — AUDIT/MAINTAIN, security tools, reports, QA

| ID | Задача | Трассировка | Pri |
| --- | --- | --- | --- |
| B-600 | Адаптеры: Semgrep, Gitleaks, Trivy, OSV-Scanner | §4, §10 | P0 |
| B-601 | Адаптеры: PHPStan, Pint, ESLint, TypeScript, Ruff, mypy | §10 | P0 |
| B-602 | Адаптеры: go test, golangci-lint, cargo test, Clippy | §10 | P0 |
| B-603 | Адаптеры: Playwright, k6 | §10 | P1 |
| B-604 | Security Agent → уязвимости, evidence, рекомендации | §6 | P0 |
| B-605 | QA Agent → тест-кейсы, результаты, coverage | §6 | P0 |
| B-606 | Performance Agent → bottlenecks, воспроизводимые бенчмарки (k6) | §6 | P1 |
| B-607 | Maintenance Agent → план исправлений, проверяемые PR | §6 | P0 |
| B-608 | DevOps Agent → CI/CD, release plan, rollback | §6 | P1 |
| B-609 | AUDIT: read-only по умолчанию, находки с severity и evidence (Laravel+React пилот) | FR-06, AC-02 | P0 |
| B-610 | MAINTAIN: triage, dependency updates, регрессионные тесты | FR-07 | P0 |
| B-611 | Prompt-injection тесты (README/issue/diff) — агент не получает прав | AC-07 | P0 |
| B-612 | `ai-coding audit`, `ai-coding maintain` (полные) | §14 | P0 |

## S8 — Self-Hosted API, RBAC, Web UI, observability

| ID | Задача | Трассировка | Pri |
| --- | --- | --- | --- |
| B-700 | FastAPI: endpoints ТЗ §12, OpenAPI, Idempotency-Key | §12, §13 | P0 |
| B-701 | Аутентификация, токены API, RBAC на сервере, межпроектная изоляция | §3, NFR-04, AC-08 | P0 |
| B-702 | Rate limiting, security headers, TLS через reverse proxy | NFR-04 | P0 |
| B-703 | PostgreSQL профиль, БД-очередь SKIP LOCKED, Redis-адаптер (опционально) | FR-13, ADR-0013 | P0 |
| B-704 | Worker(ы), ≥10 параллельных заданий, лимит конфигурируемый | NFR-02 | P0 |
| B-705 | MinIO ArtifactStore | §4 | P1 |
| B-706 | Web UI (React+TS+Vite): проекты, runs, approvals, findings, audit | §4 | P1 |
| B-707 | Метрики Prometheus, trace/run_id, OpenTelemetry (опц.) | NFR-07 | P1 |
| B-708 | docker-compose, Dockerfile (non-root, distroless/slim), backup БД, ротация ключей | §15 | P0 |
| B-709 | Экспорт данных проекта и удаление по retention | NFR-08 | P0 |

## S9 — Hardening, load/security tests, docs, release

| ID | Задача | Трассировка | Pri |
| --- | --- | --- | --- |
| B-800 | Нагрузочные тесты (k6) API/worker | NFR-02 | P0 |
| B-801 | Security-тесты: SAST/DAST API, fuzzing конфигурации, тесты sandbox escape | §12 | P0 |
| B-802 | SBOM (CycloneDX), подписанные артефакты (Sigstore), provenance (SLSA) | §12 | P0 |
| B-803 | Эксплуатационная инструкция, runbook восстановления | §19 | P0 |
| B-804 | Приёмочные сценарии AC-01…AC-10 автоматизированы | §17 | P0 |
| B-805 | Усиленная изоляция (gVisor) для Self-Hosted | §12 | P2 |
| B-806 | Релиз v2.1: changelog, теги, публикация пакета | §16 | P0 |

---

## Трассировка FR/NFR/AC → backlog

| Требование | Backlog |
| --- | --- |
| FR-01 | B-030, B-034, B-204 |
| FR-02 | B-018 |
| FR-03 | B-401…B-404, B-420 |
| FR-04 | B-500…B-503 |
| FR-05 | B-035, B-504 |
| FR-06 | B-016, B-211, B-222, B-609 |
| FR-07 | B-610 |
| FR-08 | B-112, B-116, B-117 |
| FR-09 | B-114, B-121 |
| FR-10 | B-302, B-320 |
| FR-11 | B-130, B-220 |
| FR-12 | B-412, B-016 |
| FR-13 | B-700…B-704 |
| FR-14 | B-310 |
| FR-15 | B-140, B-141, B-413 |
| FR-16 | B-103, B-120 |
| FR-17 | B-016, B-032, B-131, B-301 |
| FR-18 | B-014, B-042, B-118, B-142 |
| NFR-01 | B-005 |
| NFR-02 | B-112, B-704, B-800 |
| NFR-03 | B-104, B-113 |
| NFR-04 | B-701, B-702 |
| NFR-05 | B-002, B-203 |
| NFR-06 | B-005, B-110 |
| NFR-07 | B-042, B-120, B-707 |
| NFR-08 | B-104, B-709 |
| AC-01 | B-420 |
| AC-02 | B-609 |
| AC-03 | B-031, B-032, B-301, B-502 |
| AC-04 | B-036, B-115 |
| AC-05 | B-016, B-412 |
| AC-06 | B-220, B-302 |
| AC-07 | B-611 |
| AC-08 | B-701 |
| AC-09 | B-310, B-313 |
| AC-10 | B-131, B-132, B-321 |
