# Оркестратор: руководство разработчика

Реализация ТЗ §5 (S2). Решения: [ADR-0006](adr/0006-orchestrator-fsm-dag-checkpoints.md), [ADR-0016](adr/0016-worker-leases-and-effect-ledger.md).

## Компоненты

| Компонент ТЗ | Модуль | Что делает |
| --- | --- | --- |
| Task Planner | `orchestrator/planner.py` | `StepSpec` → `Plan`: проверка ключей, зависимостей, циклов (с выводом цикла), порядка фаз, критериев успеха (JSON Schema), бюджета; топологический порядок и уровни |
| Agent Registry | `orchestrator/registry.py` | `HandlerRegistry`: регистрация обработчиков, entry points `ai_coding.handlers`, синхронизация с таблицей `agents` |
| Agent Router | `orchestrator/router.py` | Выбор по capability → политика `agents.enabled` → стоимость → доступность; детерминированный выбор (самый дешёвый, затем по имени) с объяснением отказов |
| Workflow Executor | `orchestrator/engine.py` | Параллельный DAG через asyncio с `max_parallel_tasks`, таймауты, повторы, пропуск зависимых задач при сбое |
| Context Manager | `orchestrator/context.py` | Индекс репозитория (`git ls-files` + .gitignore); исключение `.env`, ключей и `secrets/`; TF-IDF-отбор в пределах бюджета токенов; redaction и пометка содержимого как недоверенного |
| State Manager | `orchestrator/states.py` | Таблица переходов `RUN_TRANSITIONS`, идемпотентные переходы с журналом `run_events`, optimistic locking |
| Checkpoint Manager | `orchestrator/checkpoint.py` | Снимок после каждого подтверждённого шага, SHA-256-контроль целостности |
| Policy Engine | `orchestrator/policy.py` | Решения ALLOW / DENY / REQUIRE_APPROVAL с правилом и причиной: команды, сеть, запись файлов, LLM, агенты, операции |
| Approval Manager | `orchestrator/approval.py` | Запросы, TTL, решения только Owner/Maintainer, аудит отказов |
| Result Aggregator | `orchestrator/aggregator.py` | Итог run; `confidence` агента не может превратить неуспех в успех |
| Audit Logger | `orchestrator/audit.py` | Append-only `audit_events` в той же транзакции, что и изменение; redaction |
| Retry & Recovery | `orchestrator/retry.py`, `effects.py`, lease в `engine.py` | Backoff с jitter, классификация ошибок, журнал эффектов, lease/heartbeat |
| Бюджеты | `orchestrator/budget.py` | Токены, стоимость, время; учёт восстанавливается из `model_usage` при возобновлении |
| LLM-порт | `llm/` | `LLMProvider`, `GuardedLLM`: политика → бюджет → redaction → вызов → учёт → проверка JSON Schema |

## Жизненный цикл run

`CREATED → DISCOVERY (задачи фазы discovery) → PLANNED (checkpoint) → [AWAITING_APPROVAL] → RUNNING (фаза execution) → VERIFYING (фаза verification) → COMPLETED`.

| Событие | Переход |
| --- | --- |
| Задача упала (permanent или исчерпаны попытки) | → `FAILED`; зависимые задачи `SKIPPED` (каскадно) |
| Исчерпан бюджет | → `PAUSED`; `resume` возвращает в фазу, в которой run был приостановлен |
| Шаг плана с `requires_approval` | `PLANNED → AWAITING_APPROVAL` |
| Обработчик вызвал `ctx.authorize(...)` и получил REQUIRE_APPROVAL | `RUNNING → AWAITING_APPROVAL`; после решения задача перезапускается с выданным разрешением |
| Отказ или истечение approval | → `CANCELLED` |
| `run cancel` | Сразу, если нет живого worker'а; иначе кооперативно → `CANCELLED` |
| Падение worker'а | Состояние не меняется; `resume` после истечения lease продолжает с подтверждённых шагов |

## Написать обработчик

```python
from ai_coding.orchestrator import StepContext, StepResult


class MyHandler:
    name = "acme.lint"
    capabilities = frozenset({"lint.python"})
    cost_usd = 0.0

    def is_available(self) -> bool:
        return True

    async def run(self, ctx: StepContext) -> StepResult:
        ctx.authorize(ctx.policy.command("ruff", on_host=True))  # DENY/approval → исключение
        result = ctx.effect("publish", action=..., probe=...)  # не более одного раза
        ref = ctx.save_artifact("report.json", data, "application/json")  # redaction + SHA-256
        return StepResult(output={...}, evidence_refs=[ref.uri])
```

Правила:
- Статус задачи определяет оркестратор, а не обработчик. Выход проверяется `success_criteria`.
- Классы ошибок: `TransientError` — повтор с backoff; `PermanentError` (и `PolicyDeniedError`, `SuccessCriteriaError`) — без повтора; `BudgetExceededError` — пауза.
- Любой побочный эффект, видимый вне процесса, выполняется только через `ctx.effect`.
- Сторонние обработчики регистрируются через entry point группы `ai_coding.handlers`.
