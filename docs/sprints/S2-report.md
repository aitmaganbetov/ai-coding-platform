# Отчёт Sprint S2 — Orchestrator FSM, persistence, checkpoint, retry, policy

Дата: 2026-11-05 · Ветка: `claude/cool-sagan-ho737s` · Приёмка ТЗ §16: «Восстановление после прерывания; unit tests».

## 1. Результат приёмки

| Критерий | Как проверено | Результат |
| --- | --- | --- |
| Восстановление после прерывания (AC-04, NFR-03) | `tests/integration/test_recovery.py`: настоящий процесс-worker убивается `os._exit(137)` (а) после git-коммита до записи в журнал эффектов, (б) до коммита. Второй worker делает `resume` после истечения lease | ✅ ровно 1 коммит с трейлером, `attempts=2`, `main` не тронут, run `COMPLETED`. Контрольная проверка: без `probe` тест падает |
| Unit tests | 279 новых тестов (всего 457) | ✅ все проходят |
| Покрытие core ≥ 80% (NFR-06) | `pytest --cov` (миграции исключены: их проверяют тесты миграций) | ✅ 97.72% |
| Критические переходы — 100% (NFR-06) | `test_states.py`: все 100 пар `(source, target)` против независимо записанной таблицы | ✅ |

## 2. Реализованные компоненты (ТЗ §5)

| Компонент | Модуль | Ключевое |
| --- | --- | --- |
| Task Planner | `orchestrator/planner.py` | DAG, циклы с выводом пути, порядок фаз, JSON-Schema критерии успеха, бюджет, digest плана |
| Agent Registry | `orchestrator/registry.py` | Протокол `StepHandler`, entry points `ai_coding.handlers`, синхронизация с `agents` |
| Agent Router | `orchestrator/router.py` | capability → политика → стоимость → доступность; объяснение отказов |
| Workflow Executor | `orchestrator/engine.py` | asyncio, `max_parallel_tasks`, таймауты, повторы, каскадный SKIPPED |
| Context Manager | `orchestrator/context.py` | `git ls-files`, исключение секретных файлов, TF-IDF, бюджет токенов, redaction, пометка «недоверенный» |
| State Manager | `orchestrator/states.py` | Таблица переходов, идемпотентность по ключу, журнал `run_events`, optimistic locking |
| Checkpoint Manager | `orchestrator/checkpoint.py` | Снимок после каждого шага, SHA-256-проверка |
| Policy Engine | `orchestrator/policy.py` | ALLOW / DENY / REQUIRE_APPROVAL с правилом: команды, сеть, файлы, LLM, агенты, операции, `--require-approval` |
| Approval Manager | `orchestrator/approval.py` | TTL, только Owner/Maintainer, аудит отказов |
| Result Aggregator | `orchestrator/aggregator.py` | `confidence` не перекрывает неуспех |
| Audit Logger | `orchestrator/audit.py` | Append-only на уровне ORM и триггеров БД |
| Retry & Recovery | `retry.py`, `effects.py`, lease в `engine.py` | Backoff с full jitter, классификация, журнал эффектов, lease + heartbeat |
| Persistence | `persistence/` | 15 сущностей ТЗ §13 + `run_events`, `effects`, `project_members`; UUIDv7; UTC; Alembic; WAL |
| LLM-порт | `llm/` | `LLMProvider`, `GuardedLLM` (политика → бюджет → redaction → вызов → `model_usage` → JSON Schema) |
| Логи | `observability/` | structlog JSON, redaction, `run_id`/`trace_id` |
| CLI | `cli/commands/run.py` | `run discover/list/status/events/audit/resume/cancel/approve/reject` |
| Git | `git/local.py` | Трейлеры коммитов, поиск коммита по трейлеру (B-036) |

Новые ADR: [0016](../adr/0016-worker-leases-and-effect-ledger.md) (lease и журнал эффектов), [0017](../adr/0017-structured-logging.md) (журналирование). Обновлены ADR-0005, 0006, 0009. Руководство разработчика — [orchestrator.md](../orchestrator.md).

## 3. Фактически запущенные проверки (Linux, 2026-11-05)

| Проверка | Результат |
| --- | --- |
| `uv lock --check` | OK |
| `ruff check`, `ruff format --check` | без замечаний |
| `mypy` (strict, 64 файла) | Success |
| `lint-imports` (слои cli → orchestrator → llm/persistence/config → … → domain) | 2 kept, 0 broken |
| `pytest --cov`, Python 3.13 | **457 passed**, покрытие 97.72% |
| `pytest`, Python 3.12 | **457 passed** |
| Повторяемость (engine + recovery + discovery ×10) | 10/10 зелёные |
| Миграции | upgrade/downgrade/повторный upgrade; расхождение схемы и моделей = `[]` |
| Wheel в чистом venv | `init` → `run discover` → `COMPLETED`; миграции упакованы |
| Dogfooding | `ai-coding run discover` на этом репозитории → `COMPLETED`, `.ai-coding/` не попадает в `git status` |
| actionlint | без замечаний |

Матрица Windows/macOS — в CI на GitHub после push (см. PR).

## 4. Найденные и исправленные проблемы

Все найдены тестами S2 до коммита:

1. **Конфликт версий маскировался.** После неудачного flush сообщение об ошибке читало `run.id` из «отравленной» сессии, и вместо `ConcurrencyConflictError` возникал `PendingRollbackError`. Из-за этого не работал повтор перехода.
2. **Каскадный SKIPPED.** Если в одном проходе не оставалось выполняющихся задач, транзитивно зависимые задачи навсегда оставались в `PENDING`.
3. **Бесконечный цикл approval.** Задача, запросившая approval, возвращалась в `PENDING` и тут же перезапускалась.
4. **Логи ломали run.** structlog запоминал объект `sys.stderr` в момент настройки; после его закрытия (CLI-раннер, демонизация) запись в лог падала, и задача переходила в `FAILED`. Теперь поток определяется в момент записи.
5. **Отказ в approval не попадал в аудит.** Запись делалась в той же транзакции, которая откатывалась. Теперь отказ аудируется в отдельной транзакции (ТЗ §3).
6. **Короткие id прогонов совпадали.** Префикс UUIDv7 — метка времени, поэтому для коротких id используются последние 12 символов.
7. **Незакрытые соединения SQLite в CLI** — теперь закрываются при выходе из команды.

## 5. Отклонения и ограничения

- **ADR-0005:** оркестратор пока работает с ORM-сессиями напрямую, без слоя репозиториев. Отклонение записано в ADR, выделение слоя — B-106 (S8).
- **PostgreSQL** поддержан моделями и миграцией (JSONB, триггер append-only на plpgsql), но **не проверен тестами**. CI с PostgreSQL — B-107 (S8).
- **Синхронный код обработчиков** в потоках нельзя прервать принудительно: отмена и таймаут только кооперативные. Принудительный kill — задача исполнителей S3 (subprocess/Docker).
- **Lease опирается на часы:** у worker'ов на разных машинах часы должны быть синхронизированы (NTP). Для Local Lite это неважно.
- **NFR-02** (≥10 параллельных заданий на сервере) — параллелизм внутри run реализован; нагрузочные тесты сервера — S9.
- **B-020** (пользовательский конфиг `~/.config/ai-coding`) перенесён в S5, к адаптерам LLM.
- **Создавать прогоны в S2 умеет только `run discover`.** `audit`, `develop` и остальные режимы появятся по мере исполнителей и агентов (S3–S7).

## 6. План Sprint S3 — Local/Docker executor, sandbox, tool registry

Приёмка: «Безопасный локальный audit».

1. **Порт `Executor`** (`CommandSpec`/`ExecutionResult`) и контрактные тесты (ADR-0007).
2. **LocalExecutor:** argv без shell, allowlist + approval `host_command` через Policy Engine, таймаут с убийством дерева процессов, лимиты (`setrlimit` / Job Objects), ограничение и redaction вывода, env-allowlist.
3. **DockerSandboxExecutor:** `--network none`, `--read-only`, `--cap-drop ALL`, `no-new-privileges`, non-root, лимиты pids/cpu/memory, монтирование worktree, без Docker socket; образы по digest.
4. **Tool registry** (`ToolAdapter`: detect/version/command/parse) и нормализованные находки (fingerprint, severity, file/line, evidence) в `findings`.
5. **Первые адаптеры:** Gitleaks, Semgrep, Ruff (+ версии инструментов в отчёте, NFR-05).
6. **Движок Quality Gates** (PASS/FAIL/SKIPPED) и baseline.
7. **`ai-coding audit`** (read-only, без LLM): discovery → инструменты в sandbox → гейты → отчёт JSON/Markdown; проверка отсутствия egress в `local-only`.
8. **Git:** клонирование по URL в изолированный каталог (B-034), diff/patch API и rollback (B-035).
