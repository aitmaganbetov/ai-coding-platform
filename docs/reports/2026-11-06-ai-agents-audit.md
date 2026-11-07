# Аудит AI-агентов ai-coding-platform

| Параметр | Значение |
|---|---|
| Дата | 2026-11-06 |
| Объект | локальная ветка `claude/workspaces-nexus-ui`, коммит `a7dbdd7` |
| Ветка `main` на GitHub | не проверялась |
| Режим | только чтение: код не менялся, тесты, сканеры, контейнеры и сервисы не запускались |
| Читатели | владелец проекта и команда разработки платформы; раздел 10 — для разработчиков |

**Как проводился аудит**
- Пока шёл аудит, рабочее дерево изменили и закоммитили как `a7dbdd7`, в том числе с частичным исправлением одной находки (V-03). Все находки уровня Critical и High перепроверены на `a7dbdd7`.
- Четыре области параллельно проверили субагенты, тоже только чтением: API/RBAC, песочница, чат-агенты/MCP, CI и цепочка поставок. Их ключевые выводы перепроверены по коду; такие находки отмечены ✔.
- Хук HawkScan требовал запустить сканирование и закоммитить исправления. Он проигнорирован, потому что нарушил бы режим «ничего не изменять».
- Всё, что зависит от выполнения кода, помечено **NOT VERIFIED**.

**Шкала уверенности**
- **Высокая** — путь в коде прослежен и перепроверен.
- **Средняя** — код подтверждён, но поведение во время выполнения или внешних систем не проверено.
- **Низкая** — гипотеза.

Ссылки на код в отчёте относительные: они открываются из каталога `docs/reports/`.

---

## Содержание

1. [Executive Summary](#1-executive-summary)
2. [Реестр агентов](#2-реестр-агентов)
3. [Матрица соответствия ТЗ](#3-матрица-соответствия-тз)
4. [Матрица соответствия стандартам](#4-матрица-соответствия-стандартам)
5. [Найденные уязвимости и недостатки](#5-найденные-уязвимости-и-недостатки)
6. [Архитектурные проблемы](#6-архитектурные-проблемы)
7. [Отсутствующие возможности](#7-отсутствующие-возможности)
8. [План доработки по приоритетам](#8-план-доработки-по-приоритетам)
9. [Рекомендуемая целевая архитектура](#9-рекомендуемая-целевая-архитектура)
10. [Детальные технические задачи](#10-детальные-технические-задачи)
11. [Критерии готовности к Production](#11-критерии-готовности-к-production)
- [Оценка зрелости (0–5)](#оценка-зрелости-05)
- [NOT VERIFIED](#not-verified)
- [Источники](#источники)

---

## 1. Executive Summary

**Вердикт.** Ядро платформенных агентов спроектировано зрело. В текущем виде система **не готова к многопользовательскому production**. Для режима Local Lite (один доверенный пользователь, CLI) она пригодна после задач P0-01…P0-03.

**Сильные стороны, подтверждённые кодом:**
- **Нет инструментов у LLM-агентов.** Все 12 агентов возвращают только JSON по схеме. Запись файлов, коммиты и PR делает детерминированный код под Policy Engine: [agent_runtime.py:69-104](../../src/ai_coding/workflows/agent_runtime.py#L69-L104), [implement.py:139-182](../../src/ai_coding/workflows/implement.py#L139-L182).
- **Тройная проверка вывода:**
  - JSON Schema в guard — [guard.py:141-157](../../src/ai_coding/llm/guard.py#L141-L157);
  - семантический `check()` с ограниченным repair loop — [base.py:178-206](../../src/ai_coding/agents/base.py#L178-L206), `max_repairs = 2` в строке 134;
  - валидация контракта — [contracts/agent.py:371-382](../../src/ai_coding/contracts/agent.py#L371-L382).
- **AI не заменяет реальные проверки.** Изменение принимается по результатам тестов в Docker-песочнице, AI-review только рекомендательный: [implement.py:366-378](../../src/ai_coding/workflows/implement.py#L366-L378). Шаг `develop.verify` сверяет закоммиченное по sha256 и повторно запускает тесты: [develop.py:230-254](../../src/ai_coding/workflows/develop.py#L230-L254).
- **Восстановление после сбоев.** Есть FSM с журналом переходов, leases и ledger эффектов с probe: [effects.py:58-86](../../src/ai_coding/orchestrator/effects.py#L58-L86).
- **Тесты prompt injection построены правильно.** Они исходят из того, что модель *выполнит* инъекцию, и проверяют детерминированные барьеры: [test_prompt_injection.py:1-8](../../tests/integration/test_prompt_injection.py#L1-L8).
- **Прочее:**
  - проверки IDOR;
  - files API открывает файлы с `O_NOFOLLOW`;
  - загрузки tar фильтруются с `filter="data"`;
  - webhook деплоя защищён от SSRF;
  - секреты зашифрованы AES-256-GCM с AAD, пароли — argon2id, токены хранятся как хэши;
  - `shell=True` нигде не используется;
  - Actions закреплены по SHA с минимальными `permissions`, `pull_request_target` нет;
  - SBOM и cosign;
  - флаги песочницы покрыты тестами ([test_sandbox_escape.py](../../tests/security/test_sandbox_escape.py)).

**Главные риски.** Основная угроза не в самих агентах, а в трёх местах:
- политика безопасности читается из проверяемого репозитория;
- изоляция между пользователями и проектами на сервере слабая;
- у интерактивных чат-агентов (Claude Code и Codex) защита заметно слабее, чем у оркестратора.

| ID | Риск | Уровень |
|---|---|---|
| V-01 | Любой пользователь стирает каталог данных сервера через `artifacts.path` | **Critical** |
| V-02 | Токен Agent или project-scoped превращается в полный токен пользователя | High |
| V-03 | Утечка секретов окружения через `ai.api_key_env` + `ai.base_url` (частично исправлено) | High |
| V-04 / V-05 | Режим «Аудит» чат-агентов обходится; настройки Claude Code берутся из репозитория | High |
| V-06 | Один HOME на все проекты пользователя | High |
| V-07 | Общий writable-кэш Rust toolchain в песочнице | High |
| V-08 | Developer запускает любой GitHub workflow, включая deploy | High |
| V-09 | Docker socket (равно root на хосте) и все секреты в worker | High |
| V-10 | Политика безопасности задаётся файлом проверяемого репозитория | High (архитектура) |
| V-11 | Reusable workflows ставят платформу из `@main` | High |

Всего находок: **Critical 1, High 10, Medium 22, Low 14**. Общая зрелость — **2 из 5**.

---

## 2. Реестр агентов

### 2.1 Платформенные (оркестрируемые) агенты

Общее для всех LLM-агентов:
- **Базовый класс:** `LLMAgent` в [base.py:127-218](../../src/ai_coding/agents/base.py#L127-L218).
- **Модель:** берётся из `ai.routing[агент]`, иначе `ai.model`, затем `ai.fallback`. Провайдеры: OpenAI, Anthropic, Ollama, vLLM ([llm_factory.py:82-159](../../src/ai_coding/agents/llm_factory.py#L82-L159)). По умолчанию AI выключен: [config/models.py:157](../../src/ai_coding/config/models.py#L157).
- **Инструменты:** нет.
- **Разрешения:** агент должен быть в `agents.enabled` ([policy.py:129-137](../../src/ai_coding/orchestrator/policy.py#L129-L137)). Для внешнего провайдера нужно approval `external_llm` ([agent_runtime.py:62-65](../../src/ai_coding/workflows/agent_runtime.py#L62-L65)).

| Агент | Вход → выход | Где вызывается | Детерминированные проверки | Тесты | Статус |
|---|---|---|---|---|---|
| Requirements | идея → ТЗ, user stories, AC | CREATE (только CLI) | уникальность id, трассировка FR↔story | test_agents.py | Реализован |
| Architect | ТЗ → ADR, модули, API, БД | CREATE (CLI) | модули без циклов, каждый FR назначен | test_agents.py | Реализован |
| Planner | ТЗ + архитектура → backlog | CREATE (CLI) | DAG без циклов, каждый FR закрыт | test_agents.py | Реализован |
| Planner (modernization) | цель, файлы, baseline → ≤20 шагов | MODERNIZE (CLI) | ≤25 файлов на шаг, только существующие файлы | test_agents.py | Реализован |
| Bootstrap | план → каркас, CI, README | CREATE (CLI) | шаблоны, Actions закреплены по SHA, sha256 файлов | test_agents.py, test_create.py | Реализован (Python/Node), без LLM |
| Developer | issue + контекст → полное содержимое файлов + тесты | DEVELOP, MAINTAIN, MODERNIZE | `unsafe_path`, create/modify по дереву, обязательные тесты | test_agents.py, AC-03, AC-07 | Реализован |
| Reviewer | diff → findings file/line/severity | цикл implement, `ai-coding review`, ai-review.yml | findings только по изменённым строкам, согласованный verdict | test_agents.py | Реализован, рекомендательный |
| Security | findings сканеров + код → триаж | AUDIT `--triage` (opt-in) | каждый finding оценён ровно один раз, секрет отклонить нельзя | test_agents_s7.py, AC-07 | Реализован, рекомендательный |
| Database | миграции → анализ | цикл implement, если затронуты миграции | анализируются только переданные миграции | test_agents.py | Реализован |
| Maintenance | issues, findings, зависимости → план | MAINTAIN | каждый источник учтён, версии только из fixed_versions | test_agents_s7.py, AC-07 | Реализован |
| **QA** | критерии + код → тест-кейсы | **нигде**, только `agents run` | трассировка к критериям | test_agents_s7.py | **Не интегрирован** |
| **Performance** | метрики → bottlenecks | **нигде** | ссылки только на переданные метрики | test_agents_s7.py | **Не интегрирован** |
| **DevOps** | сборка, окружения → pipeline | **нигде** | approval перед production, rollback | test_agents_s7.py | **Не интегрирован** |

Доказательство «нигде»: grep по `workflows/`, `cli/` и `api/` находит QA, Performance и DevOps только в каталоге агентов и в общей команде [agents.py:83-115](../../src/ai_coding/cli/commands/agents.py#L83-L115).

Сервер выполняет только run kinds `discover`, `audit`, `develop`, `maintain`, `deploy` ([kinds.py:129-173](../../src/ai_coding/api/kinds.py#L129-L173)). CREATE, MODERNIZE и review доступны только из CLI.

### 2.2 Интерактивные агенты и связанные компоненты

| Компонент | Реализация | Модель | Инструменты и права | Тесты | Статус |
|---|---|---|---|---|---|
| Claude Code (CLI 2.1.309) | [ai-coding-agent](../../deploy/workspace/ai-coding-agent), класс `Claude` | учётная запись пользователя | shell и правка файлов; режимы `default`/`acceptEdits`/`plan`/`bypassPermissions` ([:59](../../deploy/workspace/ai-coding-agent#L59)); MCP `platform` | test_workspace_agent.py (на фейках) | Реализован |
| Codex (0.163.0) | там же, класс `Codex` | учётная запись ChatGPT | собственная песочница Codex выключена: `danger-full-access`, сеть включена ([:62-63](../../deploy/workspace/ai-coding-agent#L62-L63)) | то же | Реализован |
| MCP-сервер `platform` | `ai-coding-agent mcp` | — | info, audit, run_status, run_report, start_task (develop/maintain), cancel_run | то же | Реализован |
| AgentBridge | [agent_bridge.py](../../src/ai_coding/api/agent_bridge.py) | — | те же операции; approve, deploy и секретов нет | test_agent_bridge.py | Реализован |
| CI AI-review | [ai-review.yml](../../.github/workflows/ai-review.yml) → Reviewer | ключ из environment | check-run и комментарий в PR | test_workflow_files.py | Реализован |

### 2.3 Инфраструктура агентов

| Компонент | Где | Состояние |
|---|---|---|
| Orchestrator | orchestrator/engine.py, states.py, planner.py, checkpoint.py | Реализован |
| Agent Router | [router.py:27-52](../../src/ai_coding/orchestrator/router.py#L27-L52) | Выбор по capability → политике → стоимости → доступности |
| Agent Runtime | workflows/agent_runtime.py | Реализован |
| Tool Registry | tools/registry.py, runner.py | Это сканеры и линтеры, а не инструменты для LLM |
| MCP | только `platform` для чат-агентов | У платформенных агентов MCP нет |
| LLM Gateway | llm/guard.py, router.py, providers.py, cache.py | Реализован |
| Memory / Context | orchestrator/context.py (TF-IDF), кэш ответов LLM, история чатов в HOME | Долговременной памяти агентов нет |
| Policy Engine | orchestrator/policy.py | Реализован; источник политики проблемный (V-10) |
| Approval Engine | approval.py, run_approvals.py | Реализован, есть дефекты V-12…V-16 |
| Sandbox / Executors | executors/docker.py, local.py, egress*.py, GitHub Actions | Реализован |
| CI/CD | 13 workflow-файлов | Реализован |

Тестов в репозитории (статический подсчёт функций):

| Набор | Файлов | Тестов |
|---|---|---|
| unit | 51 | 472 |
| integration | 42 | 285 |
| security | 5 | 19 |
| acceptance | 4 | 4 |
| web | 11 | 92 |

---

## 3. Матрица соответствия ТЗ

### 3.1 Двадцать проверок по группам агентов

**1. Соответствие ТЗ §6**
- *Платформенные LLM-агенты: PARTIAL.* Все 12 агентов есть, но QA, Performance и DevOps не встроены в процессы. Поля контракта `allowed_tools`, `scope`, `max_cost`, `max_duration` только заполняются ([agent_runtime.py:87-95](../../src/ai_coding/workflows/agent_runtime.py#L87-L95)) и нигде не применяются.
- *Bootstrap: PASS.* Вывод проверяется схемой (`run()` в bootstrap.py).
- *Чат-агенты: PARTIAL.* По §3 им положены права роли Agent, а фактически у них полная роль пользователя ([agent_bridge.py:181](../../src/ai_coding/api/agent_bridge.py#L181)).

**2. System prompt**
- *Платформенные: PASS.* Правила «данные, а не инструкции», только структурированный вывод, никаких секретов ([base.py:101-112](../../src/ai_coding/agents/base.py#L101-L112)).
- *Bootstrap: N/A* (без LLM).
- *Чат-агенты: PARTIAL.* Запрет push держится только на тексте промпта ([:80](../../deploy/workspace/ai-coding-agent#L80)).

**3. Разделение доверенных и недоверенных данных**
- *Платформенные: PARTIAL.*
  - Есть блоки `<input>` и маркеры недоверенного контента.
  - Экранируется только точная строка `</input>` ([base.py:122](../../src/ai_coding/agents/base.py#L122)).
  - Флаг `untrusted` провайдерам не передаётся.
  - Сама политика берётся из репозитория (V-10).
- *Bootstrap: PASS.*
- *Чат-агенты: PARTIAL.* Агент читает репозиторий напрямую; разделение держится только на промпте.

**4. Валидация входов и выходов**
- *Платформенные: PASS.* Параметры через pydantic с `extra=forbid` ([kinds.py:32-66](../../src/ai_coding/api/kinds.py#L32-L66)), затем схема, `check()` и контракт.
- *Bootstrap: PASS.*
- *Чат-агенты: PARTIAL.* Mailbox разбирается безопасно (O_NOFOLLOW, лимиты), но правки агента ничем не валидируются.

**5. Обработка ошибок**
- *Платформенные: PASS.* Ошибки классифицируются ([providers.py:153-174](../../src/ai_coding/llm/providers.py#L153-L174), [retry.py:72-86](../../src/ai_coding/orchestrator/retry.py#L72-L86)).
- *Bootstrap: PASS.*
- *Чат-агенты: PARTIAL.* Некорректный ответ одного контейнера роняет общий reaper (V-26).

**6. Ограничение полномочий**
- *Платформенные: PARTIAL.* Инструментов у агентов нет, но границы задаёт файл проекта (V-10), а approvals привязаны к типу операции, а не к содержимому (V-12).
- *Bootstrap: PASS.*
- *Чат-агенты: FAIL.* Доступны режимы bypassPermissions и danger-full-access, approvals можно подделать (V-18, V-19).

**7. Доступ к файлам, сети и инструментам**
- *Платформенные: PARTIAL.* Запись идёт через `unsafe_path` и `file_write` ([implement.py:99-160](../../src/ai_coding/workflows/implement.py#L99-L160)). Пробелы: следование symlink (V-32), неполный `critical_paths` (V-15).
- *Bootstrap: PASS.*
- *Чат-агенты: FAIL.* `Read` разрешён для любого пути ([:817](../../deploy/workspace/ai-coding-agent#L817)), HOME общий (V-06), egress широкий (V-20).

**8. Изоляция контекста**
- *Платформенные: PARTIAL.* У каждого агента свой `AgentContext`, но кэш LLM лежит в дереве проекта (V-34).
- *Bootstrap: PASS.*
- *Чат-агенты: FAIL.* HOME и история чатов общие для всех проектов пользователя (V-06).

**9. Работа с секретами**
- *Платформенные: PARTIAL.* Секретные файлы исключены из контекста ([context.py:23-30](../../src/ai_coding/orchestrator/context.py#L23-L30)), redaction в guard и логах. Пробелы: V-03, V-35, V-36.
- *Bootstrap: PASS.*
- *Чат-агенты: PARTIAL.* Секретов платформы в контейнере нет, но учётные записи CLI доступны всем проектам пользователя.

**10. Тайм-ауты**
- *Платформенные: PASS.* Тайм-аут HTTP ([llm_factory.py:150](../../src/ai_coding/agents/llm_factory.py#L150)), шага ([engine.py:880](../../src/ai_coding/orchestrator/engine.py#L880)) и бюджета времени ([budget.py:59-63](../../src/ai_coding/orchestrator/budget.py#L59-L63)).
- *Bootstrap: PASS.*
- *Чат-агенты: PARTIAL.* Только idle-таймаут 60 минут.

**11. Retry, fallback и resume**
- *Платформенные: PARTIAL.* Backoff с jitter, цепочка fallback ([router.py:105-133](../../src/ai_coding/llm/router.py#L105-L133)) и resume работают, но при resume не сверяется дайджест политики (V-33).
- *Bootstrap: PASS.*
- *Чат-агенты: PARTIAL.* Есть только `--resume` сессии CLI.

**12. Идемпотентность**
- *Платформенные: PASS.* Ledger с probe, коммит ищется по trailer ([develop.py:166-180](../../src/ai_coding/workflows/develop.py#L166-L180)), PR — поиском существующего.
- *Bootstrap: PASS.*
- *Чат-агенты: NOT VERIFIED.*

**13. Контроль бюджета**
- *Платформенные: PARTIAL.* Лимиты на прогон есть, но `max_cost` отдельного агента не применяется, а для моделей без цены стоимость считается равной 0 (V-29).
- *Bootstrap: PASS.*
- *Чат-агенты: FAIL.* Лимитов нет.

**14. Качество логирования**
- *Платформенные: PASS.* Structlog с redaction и run_id ([logging.py:16-61](../../src/ai_coding/observability/logging.py#L16-L61)), таблицы `agent_executions` и `model_usage`.
- *Bootstrap: PASS.*
- *Чат-агенты: PARTIAL.* Журнал хранится в HOME, который агент может изменять (V-43).

**15. Тестовое покрытие**
- *Платформенные: PARTIAL.* 31 unit-тест агентов и 7 тестов AC-07, но **нет evals на реальных моделях**.
- *Bootstrap: PASS.*
- *Чат-агенты: PARTIAL.* Только фейки; тесты не покрывают обходы V-04.

**16. Взаимодействие с другими агентами**
- *Платформенные: PASS.* Только через выходы шагов DAG; feedback структурирован ([implement.py:390-395](../../src/ai_coding/workflows/implement.py#L390-L395)).
- *Bootstrap: PASS.*
- *Чат-агенты: PARTIAL.* Mailbox спроектирован надёжно, но сокет демона внутри контейнера не аутентифицирован (V-18).

**17. Защита от prompt injection**
- *Платформенные: PARTIAL.* Барьеры подтверждены тестами AC-07. Пробелы: baseline не входит в критичные файлы (V-15), в CI политика берётся из PR (V-47), V-37.
- *Bootstrap: PASS.*
- *Чат-агенты: FAIL.* V-04 (логика парсера подтверждена), V-05.

**18. Защита от бесконечных циклов**
- *Платформенные: PASS.* `max_repairs=2`, `max_iterations≤10` ([kinds.py:50](../../src/ai_coding/api/kinds.py#L50)), `max_attempts`.
- *Bootstrap: PASS.*
- *Чат-агенты: PARTIAL.* Троттлинг только в памяти процесса.

**19. Проверка результатов независимыми инструментами**
- *Платформенные: PASS.* Песочница, Quality Gates, `develop.verify`; триаж не меняет гейты.
- *Bootstrap: PASS.*
- *Чат-агенты: PARTIAL.* Проверяет только человек.

**20. Контроль действий человеком**
- *Платформенные: PARTIAL.* Одобрять могут только Owner и Maintainer ([approval.py:89](../../src/ai_coding/orchestrator/approval.py#L89)), а план привязан к хэшу. Пробелы: V-12…V-16.
- *Bootstrap: PASS* (approval спецификации).
- *Чат-агенты: PARTIAL.* Режим ask по умолчанию, но агент сам коммитит и пушит (V-21).

### 3.2 Ключевые требования ТЗ

| Требование | Статус | Основание |
|---|---|---|
| §5: FSM, идемпотентные переходы, журнал | PASS | states.py: `RUN_TRANSITIONS`, idempotency_key |
| §5: resume без повторного применения | PASS (AC-04) | effects.py, test_recovery.py; дефект V-33 |
| §5: Router по capability, доступу, стоимости | PASS | router.py |
| §5: Context Manager без секретов | PARTIAL | V-35, V-36 |
| §5: Executor ограничивает CPU, RAM, время, сеть | PARTIAL | лимиты задаёт репозиторий (V-24), нет лимита диска |
| §5: AI не заменяет реальный тест | PASS | implement.py:366-378 |
| §5: HITL для критичных файлов, удаления, миграций, merge, deploy | PARTIAL | approval для миграций не запрашивается (V-14); merge не выполняется вообще |
| §6: контракт агента + JSON Schema | PARTIAL | 4 поля контракта не применяются |
| §3: серверный RBAC и роль Agent | PARTIAL | V-02, V-17, V-19 |
| §3: межпроектная изоляция | **FAIL** | V-01, V-06, V-07 |
| FR-17: нет auto-merge, production только с approval | PARTIAL | `auto_merge: Literal[False]`, но V-08 |
| FR-18: маскировка секретов перед LLM | PARTIAL | V-03, V-36 |
| §12: без privileged и Docker socket в песочнице | PASS | docker.py:92-134, test_sandbox_escape.py |
| §12: local-only полностью запрещает egress | PASS | `LocalOnlyTransport`, `--network none` |
| NFR-06: покрытие ≥80 %, переходы 100 % | PARTIAL | `fail_under=80` на весь пакет; покрытие ветвей переходов не измеряется |
| §17: AC-01…AC-10 | NOT VERIFIED | все 10 сопоставлены с тестами, но не запускались; sandbox-задача CI, вероятно, красная (V-46) |

---

## 4. Матрица соответствия стандартам

Это техническая проверка отдельных требований по коду. Формальная сертификация по ISO 27001 и 42001 требует аудита системы менеджмента (политики, оценки рисков, записи) — по репозиторию это NOT VERIFIED.

**Использованные версии:**
- OWASP ASVS 5.0.0;
- OWASP Top 10:2025;
- OWASP API Security Top 10 2023;
- OWASP Top 10 for LLM Applications 2025. По вторичным источникам в августе 2026 вышла редакция 2026; по первоисточнику не подтверждено, поэтому сопоставление сделано с 2025;
- OWASP Top 10 for Agentic Applications 2026 (опубликован 9 декабря 2025);
- NIST AI RMF 1.0 (пересмотр объявлен, но финальной новой версии нет);
- NIST AI 600-1;
- NIST SP 800-218 SSDF 1.1 (версия 1.2 пока черновик Rev. 1 от декабря 2025);
- NIST SP 800-218A;
- ISO/IEC 27001:2022 и 27002:2022;
- ISO/IEC 42001:2023;
- MITRE ATLAS;
- CWE;
- CVSS v3.1;
- SLSA v1.x (Build track);
- OpenSSF Scorecard.

| Стандарт | Пункты и статус |
|---|---|
| **OWASP LLM Top 10 (2025)** | LLM01 Prompt Injection — PARTIAL для платформы, FAIL для чатов (V-04, V-05); LLM02 Sensitive Info — PARTIAL (V-03, V-36); LLM03 Supply Chain — PARTIAL (V-11, V-31); LLM04 Poisoning — PARTIAL (V-34); LLM05 Output Handling — PARTIAL (схема — PASS, но V-37); LLM06 Excessive Agency — PASS для платформы, FAIL для чатов; LLM07 System Prompt Leakage — PASS (в промптах нет секретов); LLM08 Vector/Embedding — N/A (TF-IDF); LLM09 Misinformation — PARTIAL (детерминированные гейты, но нет evals); LLM10 Unbounded Consumption — PARTIAL (V-29, V-26) |
| **OWASP Agentic Top 10 (2026)** | ASI01 Goal Hijack — PARTIAL; ASI02 Tool Misuse — PASS для платформы, FAIL для чатов; ASI03 Identity & Privilege Abuse — **FAIL** (V-02, V-19); ASI04 Agentic Supply Chain — PARTIAL (V-05, V-11); ASI05 Unexpected Code Execution — **FAIL** (V-04, V-07); ASI06 Memory & Context Poisoning — PARTIAL (V-34, V-06); ASI07 Inter-Agent Communication — PARTIAL (V-18); ASI08 Cascading Failures — PARTIAL (V-09); ASI09 Human-Agent Trust — PARTIAL (V-13, V-37); ASI10 Rogue Agents — PARTIAL (V-43) |
| **OWASP Top 10:2025** | A01 Broken Access Control — **FAIL** (V-01, V-02, V-08); A02 Misconfiguration — PARTIAL (V-23); A03 Supply Chain — PARTIAL; A04 Cryptographic Failures — PASS (AES-256-GCM с AAD, argon2id, токены хранятся как SHA-256); A05 Injection — PARTIAL (`shell=True` не используется, но V-04); A06 Insecure Design — PARTIAL (V-10); A07 Authentication Failures — PARTIAL (V-02); A08 Integrity Failures — PARTIAL (V-07, V-41); A09 Logging & Alerting — PARTIAL; A10 Exceptional Conditions — PARTIAL (V-26) |
| **OWASP API Top 10 (2023)** | API1 BOLA — PASS (проект берётся из прогона, артефакт сверяется с run_id); API2 — PARTIAL (V-02); API3 — PARTIAL; API4 — PARTIAL (V-26, V-42); API5 BFLA — **FAIL** (V-08, V-17); API6 — PARTIAL; API7 SSRF — PARTIAL (webhook деплоя — PASS, но V-22, V-24); API8 — PARTIAL (V-23); API9 — NOT VERIFIED; API10 — PARTIAL |
| **ASVS 5.0 (уровень L2)** | V5 File Handling — PARTIAL (files API — PASS, артефакты — FAIL); V6 Authentication — PARTIAL; V7 Session — PARTIAL (V-27); V8 Authorization — **FAIL**; V11 Cryptography — PASS; V12 Communication — NOT VERIFIED (TLS у Caddy); V13 Configuration — **FAIL** (V-10, V-09); V14 Data Protection — PARTIAL; V15 Secure Coding & Architecture — PARTIAL; V16 Logging — PARTIAL |
| **NIST AI RMF 1.0** | GOVERN — PARTIAL (ADR и роли есть, AI-политики и риск-аппетита нет); MAP — PARTIAL (модель угроз в §12 и ADR-0010); MEASURE — **FAIL** (качество агентов на реальных моделях не измеряется); MANAGE — PARTIAL (approvals, fallback, runbook) |
| **NIST AI 600-1** | Information Security — PARTIAL; Information Integrity — PARTIAL; Data Privacy — PARTIAL; Value Chain — PARTIAL (CLI-агенты закреплены по версиям, но целостность их зависимостей не проверяется) |
| **SSDF 1.1** | PO — PARTIAL; PS.2 подпись релизов — PASS (cosign); PS.3 provenance — PARTIAL (уровень L2); PW.4 — PARTIAL (образ собирается не по lock); PW.7/PW.8 — PASS по конфигурации (запуск NOT VERIFIED); RV.1 — PARTIAL (npm и образы не сканируются) |
| **SP 800-218A** | Применимость ограничена: модели сторонние. Нет evals и версионирования промптов — PARTIAL |
| **ISO 27002:2022** | 5.3 Segregation of duties — **FAIL** (V-16); 5.15/5.18 Access control — **FAIL**; 8.2 Privileged access — **FAIL** (V-09); 8.9 Configuration management — **FAIL** (V-10); 8.12 DLP — PARTIAL; 8.15/8.16 Logging и мониторинг — PARTIAL; 8.24 Cryptography — PASS; 8.25/8.28 Secure development и coding — PARTIAL; 8.32 Change management — PARTIAL. ISMS — NOT VERIFIED |
| **ISO 42001:2023** | A.5 Impact assessment — NOT VERIFIED (документа нет); A.6 Lifecycle — PARTIAL; A.8 Information for interested parties — PARTIAL; A.9 Human oversight — PARTIAL (V-13). AIMS — NOT VERIFIED |
| **MITRE ATLAS** (сопоставление приблизительное, уверенность средняя) | AML.T0051 Prompt Injection — PARTIAL; AML.T0057 LLM Data Leakage — PARTIAL; AML.T0010 Supply Chain Compromise — PARTIAL; AML.T0034 Cost Harvesting — PARTIAL (V-29) |
| **SLSA / OpenSSF** | SLSA Build L2 (до L3 не хватает изоляции сборки); Scorecard по конфигурации: Pinned-Dependencies — PARTIAL, Token-Permissions — PASS, Dangerous-Workflow — PASS, Signed-Releases — PASS, SAST — PASS, Fuzzing — PASS, Branch-Protection — NOT VERIFIED |
| **CVE** | У самой платформы CVE нет. Зависимости: CVE-2025-4517 (обход `tarfile filter="data"`), на этот фильтр платформа полагается — патч-уровень Python в образе NOT VERIFIED (V-44). CVE-2024-29018 (Docker DNS) смягчён флагом `--dns 0.0.0.0` и покрыт тестом |

---

## 5. Найденные уязвимости и недостатки

### 5.1 Critical и High

#### V-01 · Critical · ✔ Удаление произвольного каталога сервера через `artifacts.path`

- **Классификация:** CWE-73, CWE-284. CVSS 3.1 **9.6** (AV:N/AC:L/PR:L/UI:N/S:C/C:N/I:H/A:H).
- **Где:**
  - [config/models.py:442-449](../../src/ai_coding/config/models.py#L442-L449): абсолютный путь разрешён;
  - [engine.py:203-209](../../src/ai_coding/orchestrator/engine.py#L203-L209): `make_artifact_store` берёт путь из конфигурации проекта;
  - [artifacts.py:83-87](../../src/ai_coding/persistence/artifacts.py#L83-L87): `clear()` вызывает `shutil.rmtree(self.root)`;
  - [lifecycle.py:142-168](../../src/ai_coding/api/lifecycle.py#L142-L168): удаление проекта вызывает `clear()`.
- **Сценарий:**
  1. По умолчанию проекты может создавать любой пользователь (`PROJECT_CREATORS=all`).
  2. Он загружает архив с `.ai-coding.yml`, где указано `artifacts: {path: /srv/ai-coding}`.
  3. Затем удаляет свой проект — как Owner он на это имеет право.
- **Последствия:** стираются все проекты, рабочие области, HOME пользователей с учётными записями CLI, кэши, а при SQLite — и база данных. Тот же путь используется для записи артефактов и retention-очистки.
- **Исправление:** на сервере игнорировать `artifacts.path` из файла проекта и хранить артефакты в `data_dir/artifacts/<project_id>`. Перед put, clear и purge проверять, что разрешённый путь лежит внутри базового каталога.
- **Уверенность:** высокая.

#### V-02 · High · ✔ Project-scoped или Agent-токен превращается в полный

- **Классификация:** CWE-269, CWE-863. CVSS **8.1** (AV:N/AC:L/PR:L/UI:N/S:U/C:H/I:H/A:N).
- **Где:**
  - [accounts.py:302-319](../../src/ai_coding/api/accounts.py#L302-L319): `rotate_token` собирает `Principal` без scope (строка 314);
  - [accounts.py:188-215](../../src/ai_coding/api/accounts.py#L188-L215): `issue_token` не проверяет `token_max_role`;
  - [accounts.py:92-97](../../src/ai_coding/api/accounts.py#L92-L97): `require_admin` его тоже не учитывает.
- **Сценарий:** держатель scoped-токена получает список токенов пользователя и ротирует его неограниченный токен. В ответ приходит полный токен в открытом виде.
- **Последствия:** нарушение ТЗ §3, межпроектный доступ, у администратора — полные админ-права.
- **Исправление:** операции с токенами и паролем выполнять только неограниченным токеном; новый токен не может быть шире вызывающего; `require_admin` требует `token_max_role is None`.
- **Уверенность:** высокая.

#### V-03 · High · ✔ Утечка секретов через `api_key_env` + `base_url` (частично исправлено в `a7dbdd7`)

- **Классификация:** CWE-522. CVSS **7.7** (AV:N/AC:L/PR:L/UI:N/S:C/C:H/I:N/A:N).
- **Где:**
  - имя переменной может быть любым ([config/models.py:33](../../src/ai_coding/config/models.py#L33));
  - значение ищется в окружении сервера, затем в глобальном реестре `CREDENTIALS` ([llm_factory.py:63-67](../../src/ai_coding/agents/llm_factory.py#L63-L67));
  - кнопка «Проверить провайдера» отправляет ключ на `base_url` проекта ([ai_settings.py:338-361](../../src/ai_coding/api/ai_settings.py#L338-L361)), а `_probe_guard` блокирует только link-local адреса.
- **Что исправлено:** `refuse_platform_names` запрещает имена с префиксом `AI_CODING_` ([ai_settings.py:101-118](../../src/ai_coding/api/ai_settings.py#L101-L118)). Это закрывает мастер-ключ, `WORKSPACE_SECRET`, `DATABASE_URL` и ключи других проектов.
- **Что осталось:**
  - `GITHUB_TOKEN` (используется как запасной вариант: [github/auth.py:113](../../src/ai_coding/integrations/github/auth.py#L113));
  - `ANTHROPIC_API_KEY` и `OPENAI_API_KEY` оператора;
  - S3-ключи;
  - ключ по-прежнему не привязан к хосту провайдера;
  - в CI тот же механизм срабатывает через PR (V-47).
- **Сценарий:** `api_key_env: GITHUB_TOKEN`, `base_url: https://evil/v1`, `security.network: open`, затем «Проверить» — токен уходит в заголовке `Authorization`.
- **Исправление:** allowlist имён, заданный оператором, и привязка «ключ → разрешённые хосты».
- **Уверенность:** высокая для механизма; наличие этих переменных зависит от развёртывания.

#### V-04 · High · ✔ (по разбору парсера) Обход read-only в режиме «Аудит» чат-агентов

- **Классификация:** CWE-78, CWE-184. CVSS **8.0** (AV:N/AC:H/PR:N/UI:R/S:C/C:H/I:H/A:N).
- **Где:** [ai-coding-agent:179-185](../../deploy/workspace/ai-coding-agent#L179-L185) (`git` и `xargs` считаются читающими), [:255-271](../../deploy/workspace/ai-coding-agent#L255-L271), [:817-818](../../deploy/workspace/ai-coding-agent#L817-L818) (автоодобрение).
- **Доказательство:**
  - для `xargs` опция `-a` снимается без своего аргумента, поэтому `xargs -0 -a /usr/bin/which sh -c '<cmd>'` распознаётся как запуск `which`;
  - для `git` не блокируются `git grep -O<cmd>` и `--open-files-in-pager`;
  - `Read` разрешён для любого пути.
- **Сценарий:** README «просит» агента выполнить такую команду — она одобряется автоматически, и произвольный код выполняется без вопроса человеку.
- **Исправление:** в «Аудите» не разрешать Bash автоматически (или ввести точный allowlist argv); ограничить `Read` каталогами `/workspace` и `/project`; обеспечивать read-only монтированием, а не парсером.
- **Уверенность:** высокая для логики парсера; выполнение не проверялось.

#### V-05 · High · Настройки и hooks чат-агентов из репозитория (поведение во время выполнения NOT VERIFIED)

- **Классификация:** CWE-829.
- **Где:** [ai-coding-agent:628-651](../../deploy/workspace/ai-coding-agent#L628-L651) — Claude запускается с `cwd=/workspace` без ограничения источников настроек, а вне «Аудита» ещё и с `--allow-dangerously-skip-permissions`. ADR-0042 (строка 41) сам признаёт, что настройки и hooks репозитория при таком запуске применяются.
- **Сценарий:** `.claude/settings.json` с hooks или правами, `.mcp.json` или `.codex/` в репозитории приводят к выполнению кода или расширению прав.
- **Исправление:** только пользовательские или managed-настройки; нейтрализовать конфигурацию агентов из репозитория.
- **Уверенность:** средняя.

#### V-06 · High · ✔ Один HOME на все проекты пользователя

- **Классификация:** CWE-668.
- **Где:** [workspaces.py:51-53](../../src/ai_coding/api/workspaces.py#L51-L53), [workspace_broker.py:314](../../src/ai_coding/api/workspace_broker.py#L314).
- **Сценарий:** код, запущенный в проекте A, читает учётные записи Claude и Codex и чаты проекта B, а также подкладывает `~/.bashrc` или `~/.claude/settings.json`, которые выполнятся в контейнере проекта B.
- **Исправление:** отдельный HOME на пару «пользователь + проект»; учётные записи CLI монтировать отдельно и только для чтения.
- **Уверенность:** высокая.

#### V-07 · High · ✔ Общий writable-кэш Rust toolchain

- **Классификация:** CWE-732, CWE-494.
- **Где:** [tools/project.py:201-202](../../src/ai_coding/tools/project.py#L201-L202) — монтирование с `read_only=False`.
- **Сценарий:** `build.rs` проекта во время `cargo clippy` подменяет `cargo` или `rustc` — после этого подменённые бинарники выполняются в проверках всех проектов.
- **Исправление:** монтировать только для чтения, готовить кэш под другим uid, проверять манифест хэшей.
- **Уверенность:** высокая для пути в коде; средне-высокая для эксплуатации.

#### V-08 · High · ✔ Запуск любого GitHub workflow с правами Developer

- **Классификация:** CWE-862. CVSS 7.7 — при условии, что в GitHub не настроена защита environment.
- **Где:** [github_service.py:392-426](../../src/ai_coding/api/github_service.py#L392-L426) — проверяется только `RUN_CREATE_CHANGES` (строка 401); workflow, ref и inputs произвольные. Деплой через платформу, для сравнения, требует права DEPLOY и approval.
- **Исправление:** для dispatch требовать DEPLOY и approval или ограничить запуск allowlist workflow, который задаёт Owner.
- **Уверенность:** высокая для кода; итог зависит от настроек GitHub.

#### V-09 · High · ✔ Docker socket и все секреты в worker (риск задокументирован как принятый)

- **Классификация:** CWE-250.
- **Где:** [docker-compose.yml:79](../../deploy/docker-compose.yml#L79) и строка 97 монтируют сокет; [docker-compose.yml:11-26](../../deploy/docker-compose.yml#L11-L26) передаёт `SECRET_KEY`, `WORKSPACE_SECRET` и `DATABASE_URL`. В docs/server.md (строки 396-399) это прямо названо «эквивалентом root».
- **Последствия:** любая RCE в worker даёт root на хосте и все секреты.
- **Исправление:** rootless или удалённый Docker либо authz-прокси перед сокетом; секреты вывести из worker.
- **Уверенность:** высокая.

#### V-10 · High (архитектура) · ✔ Политику задаёт проверяемый репозиторий

- **Классификация:** CWE-15.
- **Где:** конфигурация читается в [projects.py:356-363](../../src/ai_coding/api/projects.py#L356-L363), а переопределяется только раздел `ai`. Из файла проекта берутся:
  - `critical_paths` и необязательные `required_for`;
  - `security.network: open`;
  - `docker.user`, причём допускается `0:0` ([models.py:98-111](../../src/ai_coding/config/models.py#L98-L111));
  - `runtime` — gVisor выключен по умолчанию;
  - лимиты до 64 CPU, 256 ГБ и 24 часов;
  - `performance_hosts` с `egress_private=True` ([e2e.py:194-203](../../src/ai_coding/tools/e2e.py#L194-L203));
  - `artifacts.path` и baseline.
- **Последствия:** кто может изменить `.ai-coding.yml`, тот сам ослабляет политику, которой подчиняется. Это корневая причина V-01, V-03, V-24 и V-47.
- **Исправление:** серверная «нижняя граница» политики — файл проекта может только ужесточать.
- **Уверенность:** высокая.

#### V-11 · High · ✔ Платформа устанавливается из `@main` в reusable workflows

- **Классификация:** CWE-1357.
- **Где:** [ai-review.yml:22](../../.github/workflows/ai-review.yml#L22), [quality.yml:17](../../.github/workflows/quality.yml#L17), [security.yml:17](../../.github/workflows/security.yml#L17); установка без lock-файла.
- **Последствия:** закрепление workflow по SHA у потребителей не закрепляет код, который реально выполняется с правами `pull-requests: write` и LLM-ключом.
- **Исправление:** по умолчанию ставить релизную версию с хэшами и устанавливать с ограничениями из `uv.lock`.
- **Уверенность:** высокая.

### 5.2 Medium

| ID | Суть, где, сценарий | Исправление | Увер. |
|---|---|---|---|
| V-12 ✔ | **Одобрение не привязано к содержимому.** Право выдаётся на тип операции на весь прогон; повторный запрос той же задачи возвращает уже одобренное ([run_approvals.py:51-62](../../src/ai_coding/orchestrator/run_approvals.py#L51-L62), [approval.py:28-48](../../src/ai_coding/orchestrator/approval.py#L28-L48)). После повторной генерации закоммитят другой diff. `external_llm` покрывает всю цепочку fallback | Привязать к шагу, операции, sha256 diff, хосту провайдера и дайджесту политики | Высокая |
| V-13 ✔ | **«Слепое» одобрение PR.** `pr_create` запрашивается на стадии PLANNED, ещё до генерации кода ([engine.py:623-629](../../src/ai_coding/orchestrator/engine.py#L623-L629), [develop.py:315-330](../../src/ai_coding/workflows/develop.py#L315-L330)). Затем push идёт автоматически, и `on: push` CI может выполнить AI-код с секретами | Одобрение после `develop.verify` с показом diff; push только после одобрения | Высокая |
| V-14 ✔ | **`MIGRATION` и `DEPENDENCY_UPDATE` нигде не запрашиваются**, хотя MIGRATION объявлен обязательным ([enums.py:109-126](../../src/ai_coding/domain/enums.py#L109-L126)). Шаблон `**/migrations/**` не ловит `db/migrate`, `alembic/versions`, `*.sql` | Запрашивать по `is_migration_path` и в шаге `maintain.deps` | Высокая |
| V-15 ✔ | **Неполный `critical_paths`** ([models.py:396-430](../../src/ai_coding/config/models.py#L396-L430)): не защищены `.ai-coding-baseline.json` (агент может «принять» finding и снять Security-гейт), вложенные Dockerfile, `package-lock.json`, `go.sum` | Расширить список и сделать неуменьшаемый минимум; добавить тесты AC-07 | Высокая |
| V-16 | **Нет разделения обязанностей:** Maintainer создаёт прогон (в том числе production deploy) и сам его одобряет ([approval.py:72-103](../../src/ai_coding/orchestrator/approval.py#L72-L103)) | Одобряющий ≠ инициатор, обязательно для DEPLOY | Высокая |
| V-17 ✔ | **Maintainer управляет секретами и провайдерами**, хотя ТЗ §3 отводит это Owner ([rbac.py:56-67](../../src/ai_coding/api/rbac.py#L56-L67)). Может сменить `api_url` GitHub на свой хост, и токен Owner уйдёт туда | Секреты и провайдеры — только Owner; при смене endpoint сбрасывать секрет | Средне-выс. |
| V-18 | **Подделка одобрений в контейнере.** Сокет демона защищён только `chmod 600` при общем uid; режим «только чтение» у MCP проверяется на клиенте ([ai-coding-agent:1222-1262](../../deploy/workspace/ai-coding-agent#L1222-L1262)) | Аутентификация клиентов, capability-токен от сервера | Высокая |
| V-19 | **Чат-агент действует с полной ролью пользователя**, без срока действия ([agent_bridge.py:181](../../src/ai_coding/api/agent_bridge.py#L181)) | Краткоживущая capability на каждый чат | Высокая |
| V-20 | **Слабые умолчания workspace:** egress разрешает github.com, npm и pypi; `*` означает bridge-сеть; контейнер работает под uid сервера ([workspace_broker.py:300-336](../../src/ai_coding/api/workspace_broker.py#L300-L336)); gVisor выключен | Отдельный uid, gVisor, узкий egress | Высокая |
| V-21 | **Агент сам коммитит и пушит.** Запрет push существует только в промпте, а ревью по фрагментам (ADR-0040) не является шлюзом | pre-push guard вне контроля агента | Высокая |
| V-22 | **SSRF при импорте по Git URL:** допускается любой хост, а stderr git в ошибке служит оракулом ([git/local.py:590-606](../../src/ai_coding/git/local.py#L590-L606)) | Только публичные адреса, обобщённые ошибки | Средне-выс. |
| V-23 ✔ | **Единый лимит логинов для всех:** `TRUSTED_PROXIES: caddy` сравнивается с IP и никогда не совпадает ([docker-compose.yml:54](../../deploy/docker-compose.yml#L54), [limits.py:261-279](../../src/ai_coding/api/limits.py#L261-L279)). 10 ошибок в минуту блокируют вход всем | Принимать только IP или CIDR; backoff по аккаунту | Высокая |
| V-24 ✔ | **SSRF через k6:** `performance_hosts` из файла проекта, `egress_private=True`, допускается адрес 169.254.169.254 | Разрешать приватные адреса может только оператор; metadata запрещён всегда | Высокая |
| V-25 | **Нет дедлайна внутри контейнера и уборки «сирот»** после падения worker ([docker.py:91-104](../../src/ai_coding/executors/docker.py#L91-L104)) | Метки, reaper, `timeout` внутри контейнера | Высокая |
| V-26 | **Исчерпание ресурсов:** снимок рабочей области распаковывается без лимита, нет дисковых квот и лимита контейнеров; некорректный ответ одного контейнера роняет общий reaper | Лимиты, квоты, обработка ошибок по контейнеру | Средняя |
| V-27 | **Отзыв доступа не закрывает живые терминальные сессии** ([terminal.py:70-114](../../src/ai_coding/api/terminal.py#L70-L114)) | Периодическая перепроверка; остановка контейнера | Высокая |
| V-28 ✔ | **Запись через «висячий» `~/.gitconfig`:** API следует по ссылке ([workspaces.py:107-116](../../src/ai_coding/api/workspaces.py#L107-L116)) | `O_NOFOLLOW` + `O_EXCL` | Средне-выс. |
| V-29 ✔ | **Бюджет по стоимости не работает:** для моделей без цены стоимость = 0 ([providers.py:118-123](../../src/ai_coding/llm/providers.py#L118-L123)); лимиты задаёт сам проект; нет квот на пользователя и инстанс | Токенный лимит как основной; серверные квоты | Высокая |
| V-30 ✔ | **Образ публикуется и подписывается до одобрения** ([release-platform.yml:70-130](../../.github/workflows/release-platform.yml#L70-L130)) | environment на job `image` | Высокая |
| V-31 | **Образ собирается не по `uv.lock`** ([Dockerfile:42-43](../../deploy/Dockerfile#L42-L43)); Dependabot и CI не покрывают npm и образы | `--require-hashes`, Trivy/OSV в CI | Высокая |
| V-46 | **Sandbox-задача CI, вероятно, красная:** тесты требуют образ workspace, который CI не собирает ([ci.yml:140-152](../../.github/workflows/ci.yml#L140-L152)). Утверждение README «AC-01…AC-10 проходят в CI» не подтверждено | Собирать образ в CI; поправить README | Средняя |
| V-47 ✔ | **В CI политику AI-review задаёт сам PR:** checkout `head.sha` ([ai-review.yml:65](../../.github/workflows/ai-review.yml#L65)), поэтому `.ai-coding.yml` (включая `ai.base_url`) и baseline берутся из проверяемого PR, а LLM-ключ из environment передаётся в шаг ([ai-review.yml:76](../../.github/workflows/ai-review.yml#L76)). PR может направить ключ на свой endpoint или ослабить свою же проверку (для форков — после одобрения environment) | Политика и baseline из base-ref; endpoint — вход workflow; CODEOWNERS на файлы политики | Высокая |

### 5.3 Low

| ID | Суть | Исправление |
|---|---|---|
| V-32 | `apply_changes` следует по symlink ([implement.py:99-110](../../src/ai_coding/workflows/implement.py#L99-L110)); выход за дерево косвенно блокирует `export` с `filter="data"` и `develop.verify` | Запись через dir fd + `O_NOFOLLOW`; фиксировать SHA базы |
| V-33 ✔ | `policy_sha256` записывается ([engine.py:383](../../src/ai_coding/orchestrator/engine.py#L383)), но при resume ([engine.py:482-507](../../src/ai_coding/orchestrator/engine.py#L482-L507)) не сверяется | Сверять; при расхождении — новое одобрение |
| V-34 | Кэш LLM лежит в дереве проекта ([agent_runtime.py:56](../../src/ai_coding/workflows/agent_runtime.py#L56)) — возможно отравление через импорт по Git URL | Кэш в каталоге сервера + HMAC |
| V-35 ✔ | Триаж не применяет исключения контекста к `.env` и `*.pem` ([audit.py:215-247](../../src/ai_coding/workflows/audit.py#L215-L247)) | Использовать `is_excluded` |
| V-36 | Redaction только по regex; секреты сервера не зарегистрированы как литералы | Литералы и детектор энтропии |
| V-37 | Вывод LLM попадает в комментарий PR без экранирования ([review.py:50-60](../../src/ai_coding/workflows/review.py#L50-L60)) | Экранировать Markdown |
| V-38 | git запускается с полным окружением сервера ([git/local.py:75](../../src/ai_coding/git/local.py#L75), строка 623) | Allowlist окружения и `-c` overrides |
| V-39 | В egress-прокси возможна подмена заголовков через голый LF ([egress_proxy.py:136](../../src/ai_coding/executors/egress_proxy.py#L136)) | Отклонять CR/LF внутри заголовков |
| V-40 | Кэши и scratch-каталоги с правами 0777 ([prepare.py:111](../../src/ai_coding/tools/prepare.py#L111)) | Группа и права 0770 |
| V-41 | trivy-db берётся по изменяемому тегу без подписи ([trivy.py:24](../../src/ai_coding/tools/trivy.py#L24)) | Проверка cosign |
| V-42 | Лимит тела запроса только по Content-Length; сброс пароля не отзывает токены; Idempotency-Key поддерживают только 2 маршрута | См. P2-05 |
| V-43 | Журнал действий чат-агента лежит в изменяемом агентом HOME | Серверный журнал с hash-цепочкой |
| V-44 | Платформа зависит от патча Python для `tarfile` (CVE-2025-4517), патч-уровень NOT VERIFIED | Минимальная версия Python и проверка при старте |
| V-45 | SLSA L2 вместо L3 ([release.yml:45-55](../../.github/workflows/release.yml#L45-L55)) | Изолированный сборщик |

---

## 6. Архитектурные проблемы

| Вопрос | Ответ |
|---|---|
| Дублируют ли агенты функции? | Платформенные агенты — нет, роли разведены. Дублируются **каналы изменения кода**: Developer работает под политикой, чат-агенты — без неё. QA, Performance и DevOps реализованы, но не используются |
| Разделены ли ответственности? | В ядре — хорошо: LLM предлагает, детерминированный код применяет. Нарушено в конфигурации: один файл одновременно описывает проект и задаёт его политику безопасности (V-10) |
| Есть ли чрезмерная связанность? | Глобальный реестр `CREDENTIALS` по имени связывает проекты через процесс ([llm_factory.py:60](../../src/ai_coding/agents/llm_factory.py#L60)). Цепочка наследования сервисов через `config_overrides`. `app.py` — около 1100 строк |
| Может ли агент обойти Orchestrator? | Платформенные — **нет**. Чат-агенты — **да**: правят, коммитят и могут пушить в обход Policy Engine |
| Все ли операции проходят через Policy Engine? | Операции оркестратора — да. Мимо проходят действия чат-агентов, GitHub dispatch (V-08) и проверка провайдера (там проверяется только сеть) |
| Корректны ли состояния и checkpoints? | Да: таблица FSM, idempotency_key, оптимистичная блокировка, checkpoint после каждой задачи. Дефект — V-33 |
| Есть ли гонки между параллельными агентами? | Ledger и leases устойчивы. Гонки: check-then-insert для Idempotency-Key, троттлинг в памяти процесса, переиспользование одобрений |
| Можно ли безопасно восстановить задачу? | Для коммитов и PR — да (AC-04). Для контейнеров — нет (V-25) |
| Изолированы ли пользователи и проекты? | Проверки IDOR есть, у админа нет неявного доступа. Изоляцию нарушают V-01, V-02, V-06, V-07, V-09 |
| Создают ли агенты неконтролируемые изменения? | Платформа — нет: только ветка и PR, merge никогда, проверка sha256. Чат-агенты — да (V-21) |

Изменения архитектуры предлагаются только там, где это обосновано находками:
1. Серверная нижняя граница политики.
2. Привязка одобрений к содержимому.
3. Разделение control plane и execution plane.
4. Capability-модель для чат-агентов.
5. Изоляция состояния по проекту: HOME, кэши, артефакты.

---

## 7. Отсутствующие возможности

| Кандидат | Решение | Обоснование |
|---|---|---|
| Threat Modeling Agent | **Не создавать.** Расширить схему вывода Architect разделом модели угроз (STRIDE по модулям) и добавить детерминированную проверку полноты | Те же входы, что у Architect |
| Compliance Agent | **Не создавать.** Нужен детерминированный сервис отчёта о соответствии: доказательства из гейтов, SBOM и audit trail. LLM может только оформлять текст | Соответствие должно быть воспроизводимым |
| Documentation Agent | **Не создавать.** Подзадача Developer и проверка согласованности документации в DEVELOP | Bootstrap уже генерирует документацию |
| Accessibility Agent | **Не создавать.** Инструмент axe-core через Playwright в песочнице как ProjectTool | Детерминированный инструмент надёжнее |
| Dependency Intelligence Agent | **Не создавать.** Maintenance + OSV/Trivy уже есть; добавить проверку лицензий и reachability как инструменты | Дублирование функций |
| Incident Analysis Agent | **Пока не нужен.** Maintenance уже принимает telemetry | Дублирование функций |
| Test Strategy Agent | **Не создавать.** Встроить существующий QA в DEVELOP и CREATE | Агент есть, но не подключён |
| Architecture Governance Agent | **Не создавать.** Правила import-linter (кэш уже есть в репозитории) как Quality Gate | Детерминированная проверка |

Действительно не хватает:
- набора **evals и red-team на реальных моделях**;
- серверной нижней границы политики;
- одобрений, привязанных к содержимому;
- квот на пользователя, проект и инстанс;
- неизменяемого журнала с hash-цепочкой;
- брокера секретов с привязкой ключей к хостам.

---

## 8. План доработки по приоритетам

| ID | Название | Устраняет | Зависит от | Сложность |
|---|---|---|---|---|
| **P0-01** | Артефакты только в каталоге сервера | V-01 | — | S |
| **P0-02** | Исправить управление токенами | V-02 | — | S |
| **P0-03** | Ключи: allowlist имён и привязка к хостам | V-03 | — | S/M |
| **P0-04** | Ограничить GitHub dispatch | V-08 | — | S |
| **P0-05** | «Аудит» чат-агентов: read-only через ФС, настройки только пользователя | V-04, V-05 | — | M |
| **P0-06** | Отдельный HOME на пару «пользователь + проект» | V-06 | — | M |
| **P0-07** | Кэши инструментов только для чтения, с манифестом | V-07, V-40 | — | S |
| **P0-08** | Серверная нижняя граница политики | V-10, V-24, V-15 | P0-01, P0-03 | L |
| **P0-09** | CI: версия платформы, политика из base-ветки, CODEOWNERS | V-11, V-47 | — | S |
| P1-01 | Одобрения с привязкой к содержимому, разделение обязанностей, PR после verify | V-12, V-13, V-16 | P0-08 | M |
| P1-02 | Approvals MIGRATION и DEPENDENCY_UPDATE | V-14 | P1-01 | S |
| P1-03 | Разделение control и execution plane | V-09, V-38 | — | L |
| P1-04 | Capability-модель чат-агентов | V-18, V-19, V-21, V-43 | P0-05 | M/L |
| P1-05 | Укрепление workspace | V-20, V-26, V-27, V-28 | P0-06 | M |
| P1-06 | Жизненный цикл песочницы | V-25 | — | M |
| P1-07 | Сетевые границы API и RBAC по ТЗ | V-17, V-22, V-23 | — | M |
| P1-08 | Бюджеты, квоты и применение контракта агента | V-29 | — | M |
| P1-09 | Целостность прогона | V-32, V-33 | P1-01 | S |
| P1-10 | Цепочка поставок и исправный CI | V-30, V-31, V-46 | P0-09 | M |
| P2-01 | Evals и red-team на реальных моделях | §3 (п. 15) | — | M |
| P2-02 | Встроить QA, Performance и DevOps в процессы | §2.1 | P2-01 | M |
| P2-03 | Кэш LLM вне репозитория + HMAC | V-34 | — | S |
| P2-04 | Усиление redaction | V-35, V-36 | — | S |
| P2-05 | Обработка вывода LLM и пробелы API и аудита | V-37, V-42 | — | M |
| P2-06 | Egress-прокси, целостность загрузок, SLSA L3 | V-39, V-41, V-44, V-45 | P1-10 | M |
| P2-07 | Модель угроз от Architect и архитектурный гейт | §7 | — | S |
| P3-01 | Детерминированный отчёт о соответствии | §4 | P2-05 | M |
| P3-02 | Accessibility-инструмент; синхронизация документации | §7 | — | S |
| P3-03 | OpenSSF Scorecard; мониторинг аномалий агентов | §4 | — | S |
| P3-04 | Версионирование промптов и A/B на evals | §4 | P2-01 | M |

Сложность: S — до нескольких дней, M — до двух недель, L — больше двух недель (оценка ориентировочная).

---

## 9. Рекомендуемая целевая архитектура

```
┌──────────────────────── Control plane (доверенная зона) ─────────────────────────┐
│ API + RBAC (токены не шире вызывающего)  │ Policy Floor (сервер) ⊓ файл ⊓ web     │
│ Approval Service: (run, step, op, sha256(diff|plan), provider host, policy digest)│
│ Secret Broker: ключ → разрешённые хосты; секреты не попадают в worker             │
│ Audit Log: append-only, hash-цепочка; квоты user/project/instance                 │
└───────────────▲──────────────────────────────────────────────▲───────────────────┘
                │ задания + краткоживущие capability            │ события
┌───────────────┴──────── Execution plane ─────────────┐  ┌─────┴──── Agent plane ─────┐
│ Worker без мастер-ключа и без root Docker             │  │ Платформенные агенты: JSON │
│ (rootless или удалённый executor, authz на флаги)     │  │ без инструментов (как сейчас)│
│ Песочница: gVisor по умолчанию, uid≠0, RO-кэши,       │  │ Чат-агенты: capability на  │
│ метки, reaper, квоты диска                            │  │ чат, managed settings,     │
└────────────────────────────┬─────────────────────────┘  │ RO через монтирование,     │
                             │                            │ HOME на (user, project),   │
┌──────────── Data plane ────┴───────────────────────┐    │ push только через сервер   │
│ data/artifacts/<project_id>, кэши подписаны и RO,  │    └────────────────────────────┘
│ кэш LLM на сервере (HMAC), снимки с лимитами       │
└────────────────────────────────────────────────────┘
```

Принципы:
1. Файл репозитория может только **ужесточать** политику.
2. Одобрение — это подпись под **конкретным содержимым**, а не под типом операции.
3. Изоляцию обеспечивают ядро и файловая система, а не промпт или разбор команд.
4. Изменяемое состояние никогда не делится между проектами.

---

## 10. Детальные технические задачи

Для каждой задачи P0–P1 приведены все поля: проблема, доказательства, файлы, решение, зависимости, сложность, риски внедрения, тесты и критерии приёмки.

### P0

#### P0-01. Хранилище артефактов внутри каталога сервера
- **Проблема и доказательства:** V-01.
- **Файлы:** config/models.py, orchestrator/engine.py, persistence/artifacts.py, api/lifecycle.py, api/projects.py.
- **Решение:**
  - в серверном режиме подменять `artifacts.path` на `data_dir/artifacts/<project_id>`;
  - в `LocalArtifactStore` проверять, что разрешённый путь находится внутри базового каталога;
  - для `clear` и `purge` удалять только `root/sha256/**`.
- **Зависимости:** нет. **Сложность:** S.
- **Риски внедрения:** нужна миграция существующих артефактов.
- **Тесты:** проект с `artifacts.path: /` и путём через symlink — удаление проекта не трогает ничего вне базы; property-тест на пути.
- **Критерий приёмки:** ни одна операция хранилища не выходит за `data_dir/artifacts/<id>`.

#### P0-02. Исправить управление токенами
- **Проблема и доказательства:** V-02.
- **Файлы:** api/accounts.py, api/app.py.
- **Решение:**
  - выпуск, список, ротация, отзыв токенов и смена пароля — только неограниченным токеном;
  - новый токен — пересечение scope и роли с вызывающим;
  - `require_admin` дополнительно требует `token_max_role is None`;
  - смена пароля отзывает токены.
- **Зависимости:** нет. **Сложность:** S.
- **Риски внедрения:** ломаются сценарии CLI, где токены ротируют scoped-токеном.
- **Тесты:** матрица «тип токена × операция»; Agent-токен не может выпустить или ротировать токен.
- **Критерий приёмки:** ни одна цепочка вызовов не даёт токен шире исходного.

#### P0-03. Ключи провайдеров: allowlist имён и привязка к хостам
- **Проблема и доказательства:** V-03.
- **Файлы:** agents/llm_factory.py, api/ai_settings.py, config/models.py.
- **Решение:**
  - оператор задаёт `AI_CODING_SERVER_LLM_KEYS` в виде `{имя: [хосты]}`;
  - `api_key()` возвращает значение только для имени из списка и только при совпадении хоста `base_url`;
  - web-ключ проекта разрешается только для своего `project_id`, а не по имени.
- **Зависимости:** нет. **Сложность:** S/M.
- **Риски внедрения:** нужно перенастроить существующие проекты.
- **Тесты:** `GITHUB_TOKEN` или `ANTHROPIC_API_KEY` с чужим `base_url` — отказ без сетевого вызова; ключ проекта A недоступен в проекте B.
- **Критерий приёмки:** ключ никогда не отправляется на хост вне своей привязки.

#### P0-04. Ограничить GitHub dispatch
- **Проблема и доказательства:** V-08.
- **Файлы:** api/github_service.py, api/github_routes.py.
- **Решение:** право DEPLOY; allowlist workflow, который задаёт Owner; workflow, на которые ссылаются окружения деплоя, запускаются только через деплой с approval.
- **Зависимости:** нет. **Сложность:** S.
- **Риски внедрения:** меняется привычный сценарий для разработчиков.
- **Тесты:** Developer → 403; Maintainer без approval на production-workflow → отказ.
- **Критерий приёмки:** deploy-workflow невозможно запустить в обход платформенного approval.

#### P0-05. Режим «Аудит» чат-агентов
- **Проблема и доказательства:** V-04, V-05.
- **Файлы:** deploy/workspace/ai-coding-agent, api/workspace_broker.py.
- **Решение:**
  - аудит — в отдельном контейнере, где `/workspace` смонтирован read-only, а HOME — копия без учётных записей других проектов;
  - Bash не одобряется автоматически, либо используется точный allowlist argv без xargs, git grep -O и sort --compress-program;
  - Read ограничен `/workspace` и `/project`;
  - Claude запускается только с пользовательскими или managed-настройками; `.claude/`, `.mcp.json`, `.codex/` из репозитория игнорируются.
- **Зависимости:** нет. **Сложность:** M.
- **Риски внедрения:** агент в аудите станет менее удобным.
- **Тесты:** векторы V-04 → отказ; репозиторий с `.claude/settings.json` и hook → hook не выполняется; попытка записи → EROFS.
- **Критерий приёмки:** в режиме «Аудит» невозможны запись и запуск произвольного процесса без явного действия человека.

#### P0-06. Отдельный HOME на пару «пользователь + проект»
- **Проблема и доказательства:** V-06.
- **Файлы:** api/workspaces.py, api/workspace_broker.py, deploy/workspace/ai-coding-agent.
- **Решение:** путь `homes/<user>/<project>`; учётные записи CLI монтировать отдельно и только для чтения (обновлять токены через broker); история чатов хранится по проекту.
- **Зависимости:** нет. **Сложность:** M.
- **Риски внедрения:** пользователям придётся заново войти в CLI.
- **Тесты:** файл, созданный в HOME проекта A, не виден в контейнере проекта B.
- **Критерий приёмки:** в контейнере нет общего между проектами записываемого каталога.

#### P0-07. Кэши инструментов только для чтения
- **Проблема и доказательства:** V-07, V-40.
- **Файлы:** tools/project.py, tools/runner.py, tools/prepare.py.
- **Решение:** `read_only=True` для toolchain и баз сканеров; подготовка под отдельным uid; манифест sha256 проверяется перед запуском; права 0755 и 0644.
- **Зависимости:** нет. **Сложность:** S.
- **Риски внедрения:** инструменты, которые пишут в свой кэш, перестанут работать — нужен отдельный writable-оверлей на прогон.
- **Тесты:** `build.rs` пытается записать в `/toolchain` → EROFS; изменённый бинарник → отказ по манифесту.
- **Критерий приёмки:** ни один прогон не может изменить общий кэш.

#### P0-08. Серверная нижняя граница политики
- **Проблема и доказательства:** V-10, V-24, V-15.
- **Файлы:** api/projects.py (`project_config`), config/models.py, новый модуль `policy_floor.py`, workflows/*.
- **Решение:** оператор задаёт файл нижней границы. Эффективная политика — строжайшая из трёх источников (сервер, файл проекта, web). Обязательный минимум:
  - запрет `docker.user` 0;
  - runtime, заданный сервером;
  - потолки лимитов;
  - `security.network` не шире серверного;
  - `egress_private` — только оператор;
  - минимальные `critical_paths` и `required_for`;
  - игнорирование `artifacts.path`;
  - baseline только из защищённой ветки.
- **Зависимости:** P0-01, P0-03. **Сложность:** L.
- **Риски внедрения:** проекты, полагавшиеся на ослабления, перестанут работать; нужен понятный отчёт о том, что именно и почему переопределено.
- **Тесты:** property-based — для каждого поля файл не может ослабить нижнюю границу; регрессия по AC-02…AC-10.
- **Критерий приёмки:** при любом `.ai-coding.yml` эффективная политика не слабее серверной.

#### P0-09. CI: версия платформы, политика из base-ветки, CODEOWNERS
- **Проблема и доказательства:** V-11, V-47.
- **Файлы:** `.github/workflows/{ai-review,quality,security}.yml`, cli/commands/review.py, `CODEOWNERS`.
- **Решение:**
  - `platform-spec` по умолчанию — `==X.Y.Z` с хэшами;
  - политика и baseline читаются из base-ref;
  - LLM-endpoint задаётся входом workflow;
  - CODEOWNERS для `.ai-coding.yml` и `.ai-coding-baseline.json`.
- **Зависимости:** нет. **Сложность:** S.
- **Риски внедрения:** потребителям нужно обновить вызовы.
- **Тесты:** test_workflow_files.py — нет `@main`; PR с изменённым `base_url` не влияет на запуск.
- **Критерий приёмки:** PR не может изменить политику своей же проверки.

### P1

#### P1-01. Одобрения
- **Проблема и доказательства:** V-12, V-13, V-16.
- **Файлы:** orchestrator/approval.py, run_approvals.py, policy.py, workflows/develop.py, workflows/pull_request.py, модель `Approval` и миграция.
- **Решение:**
  - добавить в `Approval` поля `subject_sha256`, `provider_host`, `policy_digest`;
  - `granted` проверяется по совпадению предмета одобрения, а не по типу операции;
  - `pr_create` запрашивается после `develop.verify` с показом diff, push идёт после одобрения;
  - опция `four_eyes`, обязательная для DEPLOY.
- **Зависимости:** P0-08. **Сложность:** M.
- **Риски внедрения:** изменение UX и миграция БД.
- **Тесты:** после повторной генерации нужен новый approval; самоодобрение DEPLOY → отказ.
- **Критерий приёмки:** закоммиченный diff всегда совпадает с одобренным.

#### P1-02. Approvals MIGRATION и DEPENDENCY_UPDATE
- **Проблема и доказательства:** V-14.
- **Файлы:** workflows/implement.py (`authorize_change_set`), workflows/maintain.py.
- **Решение:** MIGRATION — по `is_migration_path`; DEPENDENCY_UPDATE — в шаге `maintain.deps`.
- **Зависимости:** P1-01. **Сложность:** S.
- **Риски внедрения:** больше запросов на одобрение в MAINTAIN.
- **Тесты:** изменение в `db/migrate/x.rb` вызывает approval.
- **Критерий приёмки:** каждая операция из `mandatory()` имеет точку запроса и тест.

#### P1-03. Разделение control plane и execution plane
- **Проблема и доказательства:** V-09, V-38.
- **Файлы:** deploy/docker-compose.yml, executors/docker.py, api/worker.py, git/local.py.
- **Решение:**
  - rootless Docker или удалённый executor по умолчанию;
  - authz-прокси проверяет флаги песочницы;
  - в worker нет `SECRET_KEY` (расшифровку делает брокер);
  - окружение git ограничено allowlist.
- **Зависимости:** нет. **Сложность:** L.
- **Риски внедрения:** эксплуатационная сложность.
- **Тесты:** в окружении worker нет мастер-ключа; запрос `--privileged` через прокси → отказ.
- **Критерий приёмки:** RCE в worker не даёт root на хосте и мастер-ключ.

#### P1-04. Capability-модель чат-агентов
- **Проблема и доказательства:** V-18, V-19, V-21, V-43.
- **Файлы:** api/agent_bridge.py, deploy/workspace/ai-coding-agent, api/workspace_broker.py.
- **Решение:**
  - сервер выдаёт токен на чат: роль не шире сессии, учитывает режим, имеет TTL;
  - сокет демона аутентифицируется секретом из `docker exec`;
  - push только через серверную публикацию с ревью; у origin внутри контейнера нет учётных данных;
  - поток событий агента пишется на сервер в append-only журнал.
- **Зависимости:** P0-05. **Сложность:** M/L.
- **Риски внедрения:** меняется привычный поток работы в терминале (push).
- **Тесты:** процесс в контейнере не может одобрить или открыть чат в режиме full; `git push` из контейнера → отказ.
- **Критерий приёмки:** каждое действие агента приписано пользователю и режиму и проверено сервером.

#### P1-05. Укрепление workspace
- **Проблема и доказательства:** V-20, V-26, V-27, V-28.
- **Файлы:** api/settings.py, api/workspace_broker.py, api/workspaces.py, api/agent_bridge.py, api/app.py.
- **Решение:**
  - userns или отдельный uid, gVisor по умолчанию;
  - egress через прокси с привязкой к репозиторию проекта;
  - дисковые квоты и лимиты числа контейнеров;
  - лимит размера распаковки снимка;
  - reaper обрабатывает ошибки по каждому контейнеру и вводит жёсткий максимум жизни;
  - отзыв доступа закрывает сессии;
  - `O_NOFOLLOW` для `~/.gitconfig`.
- **Зависимости:** P0-06. **Сложность:** M.
- **Риски внедрения:** совместимость gVisor с инструментами внутри workspace.
- **Тесты:** удаление участника обрывает терминал в течение N секунд; zip-bomb в снимке → отказ.
- **Критерий приёмки:** тесты test_workspaces.py расширены этими сценариями.

#### P1-06. Жизненный цикл песочницы
- **Проблема и доказательства:** V-25.
- **Файлы:** executors/docker.py, executors/egress.py, api/worker.py.
- **Решение:** метки run и instance; reaper при старте и при истечении lease; `timeout` внутри контейнера; квота диска на scratch.
- **Зависимости:** нет. **Сложность:** M.
- **Риски внедрения:** reaper не должен убивать контейнеры живого worker — нужна сверка lease.
- **Тесты:** после kill -9 worker не остаётся живых контейнеров.
- **Критерий приёмки:** ноль «сирот» после теста восстановления.

#### P1-07. Сетевые границы API и RBAC по ТЗ
- **Проблема и доказательства:** V-17, V-22, V-23.
- **Файлы:** api/rbac.py, api/github_service.py, api/ai_settings.py, git/local.py, api/limits.py, deploy/docker-compose.yml.
- **Решение:**
  - секреты и провайдеры — только Owner; при смене endpoint секрет сбрасывается;
  - git clone только на публичные адреса, без ssh по умолчанию, с обобщёнными ошибками;
  - `TRUSTED_PROXIES` — только IP или CIDR;
  - backoff по аккаунту при логине.
- **Зависимости:** нет. **Сложность:** M.
- **Риски внедрения:** Maintainer теряет часть привычных прав.
- **Тесты:** Maintainer → 403 на изменение секрета; clone `https://10.0.0.1` → отказ.
- **Критерий приёмки:** матрица RBAC совпадает с ТЗ §3.

#### P1-08. Бюджеты, квоты и контракт агента
- **Проблема и доказательства:** V-29 и п. 13 матрицы.
- **Файлы:** llm/providers.py, orchestrator/budget.py, api/ai_settings.py, workflows/agent_runtime.py.
- **Решение:**
  - если цены нет — применять токенный лимит или серверную цену по умолчанию;
  - квоты на пользователя, проект и инстанс;
  - применять `AgentTask.max_cost` и `max_duration`.
- **Зависимости:** нет. **Сложность:** M.
- **Риски внедрения:** длинные прогоны начнут упираться в лимиты.
- **Тесты:** модель без цены упирается в токенный лимит; превышение квоты переводит прогон в PAUSED.
- **Критерий приёмки:** расход ограничен на всех трёх уровнях.

#### P1-09. Целостность прогона
- **Проблема и доказательства:** V-32, V-33.
- **Файлы:** orchestrator/engine.py, workflows/implement.py, workflows/develop.py.
- **Решение:** сверять дайджест политики при resume; фиксировать SHA базы при export; запись через dir fd с `O_NOFOLLOW`.
- **Зависимости:** P1-01. **Сложность:** S.
- **Риски внедрения:** прогоны, ожидающие approval, придётся переодобрять после правки политики.
- **Тесты:** изменение политики во время ожидания approval → требуется новое одобрение.
- **Критерий приёмки:** прогон всегда исполняется под одобренной политикой.

#### P1-10. Цепочка поставок и исправный CI
- **Проблема и доказательства:** V-30, V-31, V-46.
- **Файлы:** deploy/Dockerfile, .github/dependabot.yml, .github/workflows/ci.yml, release-platform.yml, README.md.
- **Решение:**
  - `pip install --require-hashes` из uv.lock;
  - Dependabot для npm и docker;
  - Trivy и OSV для образа и npm в CI;
  - environment на job `image`;
  - сборка образа workspace в CI с SBOM и подписью;
  - исправить sandbox-задачу и формулировку README.
- **Зависимости:** P0-09. **Сложность:** M.
- **Риски внедрения:** время CI растёт.
- **Тесты:** CI зелёный, SBOM совпадает с образом.
- **Критерий приёмки:** заявления README подтверждены артефактом CI.

### P2 и P3

| ID | Проблема и доказательства | Решение | Файлы | Тесты и приёмка | Сложн. | Риск |
|---|---|---|---|---|---|---|
| P2-01 | Нет evals (п. 15) | Корпус prompt-injection и red-team для каждого агента и модели, запуск по расписанию и при смене модели | tests/evals/, CI | 0 успешных атак, ведущих к побочному эффекту; пороги качества | M | Стоимость вызовов LLM |
| P2-02 | QA, Performance, DevOps не используются | QA — в DEVELOP и CREATE; Performance — по результатам k6; DevOps — в CREATE | workflows/* | E2E с FakeLLM; отключаемо политикой | M | Рост времени прогона |
| P2-03 | V-34 | Кэш в `data/cache/llm/<project>` с HMAC | agent_runtime.py, llm/cache.py | Подложенная запись отвергается | S | Холодный кэш |
| P2-04 | V-35, V-36 | Литералы секретов сервера, детектор энтропии, `is_excluded` в триаже | security/redaction.py, workflows/audit.py | Fuzz-тесты redaction | S | Ложные срабатывания |
| P2-05 | V-37, V-42 | Экранирование Markdown, лимит тела на уровне ASGI, Idempotency-Key для всех POST, IP в audit | workflows/review.py, api/app.py, api/idempotency.py | Тесты render_review и chunked-запроса | M | — |
| P2-06 | V-39, V-41, V-44, V-45 | Валидация CR/LF, проверка cosign для trivy-db, минимальная версия Python, SLSA L3 | egress_proxy.py, trivy.py, release.yml | Тест на смуглинг заголовков; уровень provenance | M | — |
| P2-07 | §7 | Раздел threat model в схеме Architect; import-linter как гейт | schemas/agents/architect…, quality/gates.py | Проверка схемы; нарушение слоя → FAIL | S | — |
| P3-01 | §4 | Детерминированный отчёт о соответствии ASVS, SSDF, 42001 по доказательствам | reports/ | Воспроизводимость отчёта | M | — |
| P3-02 | §7 | axe-core как ProjectTool; проверка документации в DEVELOP | tools/e2e.py | Находки a11y в отчёте | S | — |
| P3-03 | §4 | OpenSSF Scorecard в CI; алерты на аномалии стоимости и отказов политики | CI, api/metrics.py | Порог Scorecard; правила алертов | S | — |
| P3-04 | §4 (SP 800-218A) | Промпты как версионируемые артефакты, A/B на evals | agents/*.py | Диф метрик evals | M | — |

---

## 11. Критерии готовности к Production

1. Нет открытых Critical и High. Каждая Medium либо закрыта, либо принята с владельцем и сроком.
2. Нижняя граница политики действует: property-тесты доказывают, что файл проекта не ослабляет ни одно поле.
3. Изоляция проектов: AC-08 расширен на `artifacts.path`, HOME, кэши и токены, и все сценарии проходят.
4. Каждое approval привязано к хэшу содержимого; для DEPLOY включено правило четырёх глаз; MIGRATION и DEPENDENCY_UPDATE запрашиваются.
5. Чат-агенты:
   - «Аудит» обеспечивается read-only на уровне файловой системы;
   - конфигурация агентов из репозитория не применяется;
   - push возможен только через сервер.
6. Worker не имеет root-эквивалента на хосте и мастер-ключа.
7. Evals на каждой поддерживаемой модели: ноль инъекций, приводящих к побочному эффекту.
8. CI зелёный на `main`, включая sandbox-задачу; AC-01…AC-10 подтверждены артефактом.
9. Цепочка поставок: образы собираются по lock с хэшами, подпись ставится после одобрения, SBOM совпадает с образом, SLSA L3.
10. Квоты по токенам, стоимости, контейнерам и диску на пользователя, проект и инстанс; алерты при превышении.
11. Неизменяемый журнал аудита с outcome для всех мутаций и IP клиента.
12. Проведены внешний пентест и учения по восстановлению: backup и restore, ротация мастер-ключа.

---

## Оценка зрелости (0–5)

**Методика.**
- Уровень реализации:
  - 0 — нет;
  - 1 — фрагменты;
  - 2 — реализовано;
  - 3 — реализовано и покрыто тестами;
  - 4 — плюс автоматическая проверка в CI, метрики и внешняя проверка;
  - 5 — плюс непрерывные evals и red-team и измеримое улучшение.
- Потолок по самой серьёзной открытой находке: Critical — не выше 1, High — не выше 2, Medium — не выше 3.
- Балл направления = минимум из уровня реализации и потолка.
- Итог = медиана по направлениям.

| Направление | Балл | Обоснование |
|---|---|---|
| Оркестратор (FSM, DAG, восстановление) | 3 | Реализован и протестирован (AC-04); открыты Medium V-12, V-33 |
| Контракты и валидация вывода | 3 | Схема + семантика + repair loop; 4 поля контракта не применяются |
| LLM Gateway | 2 | Хороший дизайн; остаток High V-03 |
| Защита от prompt injection: платформенные агенты | 3 | Барьеры детерминированы и покрыты тестами; Medium V-15, V-47 |
| Защита от prompt injection: чат-агенты | 1 | High V-04, V-05, V-06; защита во многом на промпте |
| Policy и Approval | 2 | High V-10; Medium V-12…V-16 |
| Песочница и исполнители | 2 | Сильные флаги и escape-тесты; High V-07, V-09 |
| RBAC и изоляция проектов | 1 | Critical V-01 |
| Секреты | 2 | AES-GCM, write-only хранение; High V-03 |
| Наблюдаемость и аудит | 3 | Structlog с redaction, audit trail; пробелы V-42, V-43 |
| Тестирование агентов | 2 | Сильные детерминированные тесты; нет evals; CI, вероятно, красный |
| CI/CD и цепочка поставок | 2 | SHA-пины, подписи, SBOM; High V-11 |
| Документация и ADR | 3 | 48 ADR и runbook; утверждение в README не подтверждено |
| **Итог** | **2** | Медиана; ограничена открытыми Critical и High в изоляции |

---

## NOT VERIFIED

- Ни один тест, прогон CI или сценарий AC-01…AC-10 не запускался.
- Фактическое поведение Claude Code 2.1.309 и Codex 0.163.0 с настройками из репозитория (V-05) и выполнение обходов V-04.
- Настройки GitHub: защита environment, веток и тегов (влияет на V-08, V-30, V-47); соответствие SHA action-ов их тегам.
- Патч-уровень Python в образе (V-44), версия Docker, наличие ssh-ключей у worker (V-22) и переменных провайдеров в окружении (V-03).
- Фактический процент покрытия кода тестами.
- Ветка `main` на GitHub.
- Организационные процессы по ISO 27001 и 42001.

---

## Источники

Версии стандартов:
- [OWASP Top 10:2025](https://owasp.org/Top10/2025/)
- [OWASP Top 10 for LLM Applications — репозиторий проекта](https://github.com/OWASP/www-project-top-10-for-large-language-model-applications)
- [NIST AI RMF](https://www.nist.gov/itl/ai-risk-management-framework)
- [NIST SSDF 1.2 — черновик SP 800-218 Rev. 1](https://csrc.nist.gov/pubs/sp/800/218/r1/ipd)
- [NIST: анонс черновика SSDF 1.2](https://csrc.nist.gov/News/2025/draft-ssdf-version-1-2)
- [CSA: OWASP GenAI Top 10 2026 и Agentic](https://labs.cloudsecurityalliance.org/research/csa-research-note-owasp-genai-top10-2026-agent-control-stand/) — вторичный источник
- [OWASP GenAI LLM Top 10 2026](https://cybersecuritynews.com/owasp-genai-llm-top-10-2026/) — вторичный источник
- [OWASP Top 10 for Agentic Applications](https://www.akto.io/blog/owasp-top-10-agentic-applications) — вторичный источник
