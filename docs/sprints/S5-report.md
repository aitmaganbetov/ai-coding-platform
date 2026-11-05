# Отчёт Sprint S5 — CREATE: requirements, architect, planning, bootstrap

Дата: 2026-11-05 · Ветка: `claude/cool-sagan-ho737s` · Приёмка ТЗ §16: «Новый проект из задачи до PR» (AC-01, P0-07, P0-10).

## 1. Результат приёмки

| Критерий | Чем подтверждается | Результат |
| --- | --- | --- |
| **AC-01:** из текстового описания получить согласованное ТЗ, ADR, backlog, каркас и PR | `tests/integration/test_create.py::test_create_with_pull_request`: описание → ТЗ v1 → approval → ADR и архитектура → backlog → каркас в `ai/bootstrap` → проверка → push → PR (без merge и auto-merge) | ✅ на fake LLM API и fake GitHub API по HTTP, с настоящим git и bare-remote |
| **FR-03:** диалог требований → версия ТЗ → approval → ADR → backlog → bootstrap | Версии `v<раунд>-<sha8>`. Approval `spec_approval` на конкретную версию; отключить его нельзя. Раунд «доработать» создаёт следующую версию с ответами на вопросы | ✅ `test_revision_round_produces_next_version`, `test_interactive_approve_and_revise`, `test_revise_from_file`, `test_rejected_spec_stops_before_architecture` |
| Каркас рабочий, а не формальный | Сгенерированные проекты проходят **собственные** тесты и lint: Python — pytest + ruff, Node — `node --test`. Сгенерированный `.ai-coding.yml` валиден | ✅ `test_bootstrap_python_skeleton_passes_its_own_checks`, `test_bootstrap_node_skeleton_passes_its_own_tests`, а также pytest на клоне ветки в e2e-тесте |
| Офлайн-режим (FR-12, AC-05) | Весь CREATE на локальной модели (Ollama-совместимый API): без ключей, без approval внешнего LLM | ✅ `test_create_offline_with_local_model` |
| Защита от инъекций и утечек (ТЗ §12, FR-18) | Входы передаются как данные в `<input>`; секрет из описания маскируется до отправки; ключ не попадает ни в тело запроса, ни в кэш, ни в ошибки | ✅ `test_external_provider_needs_approval_and_key_stays_secret`, `test_secrets_never_reach_the_cache` |
| Бюджеты и маршрутизация (FR-15) | Цепочка routing → fallback; переход при сбое или невалидном ответе; пауза при исчерпании бюджета до отправки запроса; стоимость по `ai.pricing` | ✅ `test_fallback_after_primary_failure_and_repair`, `test_budget_exhaustion_pauses_the_run`, unit-тесты router |
| Идемпотентность (AC-04) | Повтор после коммита не создаёт второй коммит; существующая ветка не перезаписывается | ✅ `test_retry_after_commit_does_not_commit_twice`, `test_existing_branch_is_never_overwritten` |

## 2. Реализованные компоненты

| Компонент | Модуль | Ключевое |
| --- | --- | --- |
| LLM-адаптеры (B-410…B-412) | `llm/providers.py` | OpenAI (Chat Completions + `json_schema`); Anthropic (Messages + принудительный tool call); Ollama и vLLM (OpenAI-совместимый `/v1`); классификация ошибок; https для внешних провайдеров; цена по `ai.pricing` |
| Маршрутизация и fallback (B-413) | `llm/router.py`, `agents/llm_factory.py` | Каждый маршрут проходит свой `GuardedLLM`. Approval и бюджет останавливают цепочку. Маршрут без ключа пропускается |
| Кэш ответов (B-414) | `llm/cache.py` | Ключ по redacted-запросу; только `temperature 0`; повторная проверка схемы; без повторного списания бюджета |
| Порт `Agent` (B-400) | `agents/base.py` | Контракт ADR-0011; промпт «входы — данные»; семантические проверки; цикл исправления (≤ 2 раунда) |
| Агенты (B-401…B-404) | `agents/requirements.py`, `architect.py`, `planner.py`, `bootstrap.py` | Requirements, Architect, Planner работают через LLM; Bootstrap детерминирован (шаблоны Python и Node, CI закреплён по SHA) |
| Схемы выходов (B-405) | `schemas/agents/*.output.schema.json` | 4 схемы, JSON Schema 2020-12, входят в wheel |
| Каталог агентов (B-406) | `agents/catalog.py`, `ai-coding agents list` | 12 агентов ТЗ §6 со статусом, входом → выходом и цепочкой моделей |
| Сценарий CREATE (B-420) | `workflows/create.py` | requirements → approve → architecture → plan → bootstrap (worktree, approval критичных файлов, коммит с trailer) → verify → [push → PR → verify] |
| CLI (B-421) | `ai-coding create` | Интерактивный диалог (approve / revise / stop); `--no-interactive`, `--resume`, `--revise --answers`, `--pr`, `--dry-run`, `--require-approval` |
| Конфигурация | `config/models.py` | `ai.timeout_seconds`, `ai.cache`, `ai.pricing`; у маршрутов свои `base_url` и `api_key_env` с наследованием; проверки маршрутов; `ApprovalOperation.SPEC_APPROVAL` |
| Git | `git/local.py` | `create_root_commit` (только в пустом репозитории), `list_files`, `file_at` |
| Оркестратор | `orchestrator/engine.py`, `policy.py` | `task_output()`, `project_id` в `StepContext`, `PolicyEngine.spec_approval()`; универсальный `run resume` подсказывает `create --resume` |
| Тестовые двойники | `tests/fake_llm.py`, `tests/agent_samples.py` | Настоящий HTTP на localhost в форматах OpenAI и Anthropic; сценарии ошибок |

Новые ADR: [0023](../adr/0023-llm-adapters-routing-cache.md) (адаптеры на httpx вместо SDK — уточнение ADR-0009), [0024](../adr/0024-agents-and-create-mode.md). Руководство: [create.md](../create.md); обновлены [configuration.md](../configuration.md), [cli.md](../cli.md), backlog, архитектура и README.

## 3. Фактически запущенные проверки (Linux, Docker, 2026-11-05)

| Проверка | Результат |
| --- | --- |
| `pytest --cov`, Python 3.13 (gitleaks в PATH, Docker, node) | **689 passed, 0 skipped**, покрытие 96% |
| `pytest`, Python 3.12 | **689 passed, 0 skipped** |
| Новые тесты S5 | 21 (провайдеры, кэш, router) + 21 (агенты, bootstrap, фабрика) + 10 (сценарий CREATE) + 6 (CLI) |
| ruff, ruff format, mypy strict (118 файлов), import-linter (агенты на уровне tools/integrations) | без замечаний |
| Дрейф схемы конфигурации | нет |
| `uv lock --check` | OK (новых зависимостей нет) |
| gitleaks `dir` | no leaks found |
| Wheel | собран; содержит `schemas/agents/*.json`; `ai-coding agents list` и загрузка схем из установленного wheel работают |

Результаты CI (Windows и macOS, sandbox, пилоты) будут в PR.

## 4. Найденные и исправленные проблемы

1. **Зависимости шага видят только прямых предшественников.** Архитектор падал с `KeyError: 'create.requirements'`. Нужные рёбра DAG теперь объявлены явно.
2. **Шаг проверки каркаса стоял в фазе verification**, а push и PR — в execution. Планировщик правильно отверг план, в котором push зависит от более поздней фазы. Проверка перенесена в execution, чтобы выполняться до push.
3. **Пауза на approval расходует попытку задачи** (поведение движка из S2). Bootstrap с одним approval и одним сбоем исчерпывал лимит. Для bootstrap задано `max_attempts = 3`; само поведение движка зафиксировано в «Незавершённом».
4. **CLI не видел ожидающих approvals.** Сравнение шло с `"PENDING"`, а статус хранится как `"pending"`. Теперь сравнение идёт по перечислению.
5. **`ai-coding run approve -C <новый проект>` не находил конфигурацию**: в новом проекте `.ai-coding.yml` есть только на рабочей ветке. `create` теперь сохраняет конфигурацию прогонов в `.ai-coding/config.yml` (исключён из git, без секретов), и команды проекта используют её как запасную.
6. **Генерация Python ломалась на кавычках в описаниях модулей.** Строки и docstrings теперь пишутся как JSON-литералы — они валидны в Python при любом содержимом.
7. **Windows: `write_text` превращает `\n` в `\r\n`**, из-за чего хэши в проверке каркаса не совпали бы. Файлы каркаса пишутся байтами.
8. **vLLM без `base_url` проходил валидацию конфигурации.** Теперь это ошибка; существующий тест политики обновлён.
9. **Изменяемое значение по умолчанию (`Redactor`) в dataclass маршрутизатора** — исправлено (`default_factory`).

## 5. Ограничения и незавершённое

- **Реальные модели в этой среде не проверялись.** API-ключей нет, загрузка Ollama и моделей с Hugging Face заблокирована сетевой политикой. Адаптеры проверены против fake-серверов, повторяющих документированные форматы OpenAI и Anthropic, через настоящий HTTP. Предлагаю провести smoke-проверку на реальном провайдере, когда у вас будет ключ или локальная Ollama:

```bash
ai-coding create "…" --dest ./demo --config platform.yml
```

- **PR из `create --pr` на реальном GitHub не открывался**: проверен на fake GitHub и локальном bare-remote. Тот же PR-workflow S4 на реальном GitHub тоже пока не запускался — только пилоты (комментарии, checks, SARIF).
- **Стеки каркаса** — Python и Node.js. Для PHP, Go и Rust шаг bootstrap честно сообщает «не поддерживается» (адаптеры стеков — S7).
- **Качество ТЗ и архитектуры зависит от модели.** Проверки структурные: id, ссылки, циклы, покрытие требований. Смысл оценивает человек на approval.
- **Пауза на approval расходует попытку задачи** (п. 3 выше). Поправить в движке в S6, отдельно от CREATE.
- **`ai-review.yml`** — S6 (B-507).

## 6. План Sprint S6 — DEVELOP и MODERNIZE, AI review/fix

1. Developer Agent: issue и контекст → патч в worktree, пояснение, тесты; применение через effect ledger.
2. Reviewer Agent: замечания с file/line/severity, рекомендательные (гейт `review`).
3. DEVELOP: issue → план → ветка → patch → тесты (Docker) → review → PR (AC-03). Цикл исправления с ограничением итераций и бюджета.
4. MODERNIZE: baseline, анализ влияния, малые PR, rollback (FR-05). Database Agent.
5. `ai-coding develop`, `ai-coding modernize`; reusable `ai-review.yml` (same-repo PR или approval через environment).
6. Движок: approval-пауза не должна расходовать попытку задачи.
7. Smoke-проверка адаптеров на реальном провайдере (при наличии ключа) и PR на реальном GitHub.
