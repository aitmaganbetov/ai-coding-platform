# ADR-0011: Единый контракт агентов

- Статус: Принято (контракт реализован в S1; агенты — S5–S7)
- Дата: 2026-11-05

## Контекст
ТЗ §6: агенты Requirements, Architect, Planner, Bootstrap, Developer, Reviewer, Security, QA, Database, Performance, DevOps, Maintenance. Контракт: `task_id, input_refs, allowed_tools, scope, max_cost, max_duration, output_schema, evidence_refs, status, confidence`. Выход валидируется JSON Schema; неверный ответ не считается выполненной задачей.

## Решение
- `AgentTask` (вход) и `AgentResult` (выход) — Pydantic-модели в `ai_coding.contracts.agent`; `output_schema` проверяется как корректная JSON Schema 2020-12 при создании задачи.
- `validate_agent_output()` принимает результат только если: `task_id` совпадает, статус `succeeded`, `output` валиден по `output_schema`. Иначе задача не завершена → Retry & Recovery.
- `AgentScope` по умолчанию запрещает запись, shell и сеть.
- Каждый агент (S5+) реализует порт `Agent`: `name`, `capabilities`, `default_output_schema`, `required_tools`, `run(task, context) -> AgentResult`; обработка ошибок — исключения классифицируются (transient/permanent/policy-violation); журналирование — через Audit Logger с redaction.
- Выходные схемы агентов хранятся в `ai_coding/schemas/agents/<agent>.output.schema.json` (S5), например Reviewer — `findings[{file,line,severity,message,evidence}]`.
- `confidence` информативна и **никогда** не заменяет результаты детерминированных проверок (ТЗ §5 Result Aggregator).

## Последствия
Агенты взаимозаменяемы, их выход проверяем машинно; некорректный ответ LLM не может «пройти» как успех.

## Альтернативы
Свободный текст + парсинг — отклонено (недетерминированно, непроверяемо).
