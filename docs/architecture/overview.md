# Архитектура AI Coding Platform v2.1

Документ описывает целевую архитектуру всей платформы (S1–S9). Состояние реализации каждого модуля — в [modules.md](modules.md) и [../backlog.md](../backlog.md). Решения обоснованы в [ADR](../adr/README.md).

## 1. Контекст

```mermaid
flowchart LR
    U[Пользователь<br/>Owner / Maintainer / Developer / Auditor]
    subgraph Platform[AI Coding Platform]
        CLI[CLI ai-coding]
        API[REST API FastAPI]
        UI[Web UI React+TS]
        CORE[Core Orchestrator]
    end
    GIT[(Локальный Git)]
    GH[GitHub API / Actions]
    LLM[LLM: OpenAI / Anthropic<br/>Ollama / vLLM]
    TOOLS[Инструменты: Semgrep, Gitleaks,<br/>Trivy/OSV, линтеры, тесты]
    DOCKER[Docker sandbox]
    DB[(SQLite / PostgreSQL)]
    ART[(Артефакты: диск / MinIO)]

    U --> CLI & UI
    UI --> API
    CLI --> CORE
    API --> CORE
    CORE --> GIT
    CORE -. опционально .-> GH
    CORE -. опционально, по политике egress .-> LLM
    CORE --> DOCKER --> TOOLS
    CORE --> DB & ART
```

GitHub и LLM — **опциональны**: детерминированный AUDIT работает локально без сети (FR-12, AC-05).

## 2. Профили развёртывания (ТЗ §15)

| Профиль | Компоненты | Хранилище | Очередь |
| --- | --- | --- | --- |
| Local Lite | CLI + in-process orchestrator + Local/Docker executor + git worktree; LLM опционально | SQLite `.ai-coding/state.db`, артефакты на диске | in-process scheduler поверх БД-очереди |
| Self-Hosted | FastAPI + worker(ы) + PostgreSQL + MinIO + Web UI + Docker executor, reverse proxy с TLS | PostgreSQL, MinIO | БД-очередь (SKIP LOCKED) или Redis |
| GitHub Hybrid | Self-Hosted или CLI + GitHub App/token + reusable workflows + Rulesets | как у базового профиля | + GitHub Actions executor |

## 3. Слои и компоненты

```mermaid
flowchart TB
    subgraph Entry[Точки входа — composition root]
        cli[cli]:::done
        api[api]:::todo
        worker[worker]:::todo
    end
    subgraph App[Прикладной слой]
        workflows[workflows<br/>CREATE/DEVELOP/MODERNIZE/AUDIT/MAINTAIN]:::todo
        orchestrator[orchestrator<br/>planner, registry, router, executor,<br/>context, state, checkpoint, policy,<br/>approval, aggregator, audit, retry]:::todo
        agents[agents x12]:::todo
        quality[quality gates]:::todo
        reports[reports JSON/MD/SARIF/JUnit]:::todo
    end
    subgraph Ports[Порты и адаптеры]
        config[config]:::done
        contracts[contracts]:::done
        git[git]:::done
        detection[detection]:::done
        persistence[persistence]:::todo
        executors[executors]:::todo
        llm[llm]:::todo
        tools[tools]:::todo
        github[integrations.github]:::todo
    end
    security[security]:::done
    domain[domain]:::done

    Entry --> App --> Ports --> security --> domain
    classDef done fill:#d4f7d4,stroke:#2e7d32;
    classDef todo fill:#f5f5f5,stroke:#9e9e9e,stroke-dasharray: 4 3;
```

Зелёные — реализованы в S1, серые — запланированы. Правило зависимостей проверяется `import-linter` (ADR-0003).

## 4. Жизненный цикл run (ТЗ §4)

```mermaid
stateDiagram-v2
    [*] --> CREATED
    CREATED --> DISCOVERY
    DISCOVERY --> PLANNED
    PLANNED --> AWAITING_APPROVAL: план требует approval
    PLANNED --> RUNNING: approval не требуется
    AWAITING_APPROVAL --> RUNNING: approve
    RUNNING --> VERIFYING
    VERIFYING --> COMPLETED: все гейты PASS
    VERIFYING --> RUNNING: retry / fix
    RUNNING --> PAUSED: бюджет / ожидание / прерывание
    PAUSED --> RUNNING: resume (с checkpoint)
    CREATED --> CANCELLED
    DISCOVERY --> CANCELLED
    PLANNED --> CANCELLED
    AWAITING_APPROVAL --> CANCELLED: reject / cancel
    RUNNING --> CANCELLED
    PAUSED --> CANCELLED
    DISCOVERY --> FAILED
    RUNNING --> FAILED
    VERIFYING --> FAILED
    COMPLETED --> [*]
    FAILED --> [*]
    CANCELLED --> [*]
```

Точная таблица переходов — артефакт S2 (State Manager), покрывается тестами на 100% ветвей (NFR-06). Переходы идемпотентны и журналируются; после рестарта выполнение продолжается с последнего подтверждённого checkpoint без повторного применения изменений (ADR-0006).

## 5. Поток DEVELOP (FR-04, AC-03)

```mermaid
sequenceDiagram
    actor Dev as Developer
    participant CLI
    participant O as Orchestrator
    participant P as Policy/Approval
    participant G as Git (worktree)
    participant A as Agents (Planner, Developer, Reviewer, Security, QA)
    participant X as Executor (Docker)
    participant Q as Quality Gates
    participant H as GitHub
    Dev->>CLI: ai-coding develop --issue 42
    CLI->>O: create run (Idempotency-Key)
    O->>A: Planner → DAG задач
    O->>P: нужен ли approval?
    P-->>Dev: запрос approval (при необходимости)
    O->>G: add_worktree(ai/<run>/<task>)
    O->>A: Developer → patch (в worktree)
    O->>X: сборка и тесты в sandbox
    X-->>Q: JUnit / SARIF / coverage
    O->>A: Reviewer / Security (рекомендации)
    Q-->>O: PASS / FAIL / SKIPPED
    O->>P: approval pr_create
    O->>H: push ветки + PR (без push в main)
    H-->>Dev: PR со статусами проверок
```

## 6. Сквозные аспекты

| Аспект | Механизм | ТЗ |
| --- | --- | --- |
| Безопасность | ADR-0010: недоверенный контент, least privilege, sandbox, egress-политика, redaction | §12, AC-07 |
| Секреты | Не в YAML (валидатор S1), Redactor для логов/prompts/артефактов, gitleaks | FR-18, NFR-07 |
| Аудит | append-only `audit_events` (actor, project_id, run_id, timestamp, outcome) | §3, FR-16 |
| Наблюдаемость | structlog JSON-логи с run_id/trace_id, метрики Prometheus (S8), OpenTelemetry — опционально | NFR-07 |
| Воспроизводимость | закреплённые версии инструментов и образов (digest), версии в отчётах | NFR-05 |
| Идемпотентность | Idempotency-Key в API, ключи эффектов в оркестраторе | §12, AC-04 |
| Экспорт/удаление | `report export`, удаление проекта по retention-политике | NFR-08 |

## 7. Модель данных (ТЗ §12)

```mermaid
erDiagram
    users ||--o{ audit_events : actor
    projects ||--o{ repositories : has
    projects ||--o{ runs : has
    projects ||--o{ policies : has
    projects ||--o{ findings : has
    runs ||--o{ tasks : contains
    tasks ||--o{ task_dependencies : "depends on"
    tasks ||--o{ agent_executions : runs
    agents ||--o{ agent_executions : performs
    runs ||--o{ approvals : requires
    runs ||--o{ artifacts : produces
    runs ||--o{ checkpoints : persists
    agent_executions ||--o{ model_usage : consumes
    runs ||--o{ findings : reports
```

Все идентификаторы — UUID, время — UTC, `runs`/`tasks` имеют `version` для optimistic concurrency (ADR-0005).
