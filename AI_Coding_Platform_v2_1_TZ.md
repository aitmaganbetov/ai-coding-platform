## ТЕХНИЧЕСКОЕ ЗАДАНИЕ

AI Coding Platform v2.1 — Local, Self-Hosted & GitHub Hybrid Полный жизненный цикл AI-разработки (AI SDLC) | Проект: aitmaganbetov/ai-coding-platform Статус: проектное ТЗ для реализации | Версия документа: 2.1 | Дата: 05.11.2026

Разработать самостоятельную платформу управления жизненным циклом программных проектов: от идеи и технического задания до разработки, аудита, модернизации, выпуска и сопровождения. Платформа должна работать локально без GitHub, на собственном сервере и в интеграции с GitHub Actions. Наличие LLM-провайдера не должно быть обязательным для запуска детерминированных проверок.

## 1. Назначение и цели

## 2. Границы проекта и режимы

| Режим | Назначение | Основной результат |
| --- | --- | --- |
| CREATE | Новый проект: требования, | Репозиторий и проверяемый |
|   | архитектура, план, каркас, код | MVP |
| DEVELOP | Новые функции и устранение | Отдельная ветка/PR с тестами |
|   | дефектов |   |
| MODERNIZE | Рефакторинг, обновление | Поэтапная модернизация с |
|   | зависимостей, миграция | откатом |
| AUDIT | Технический, архитектурный, | Отчёт с доказательствами и |
|   | security и performance аудит | приоритетами |
| MAINTAIN | Плановое сопровождение и | Контролируемые изменения и |
|   | устранение регрессий | мониторинг |

Вне v2.1: полностью автономный деплой в Production без согласования, гарантированное исправление всех уязвимостей, обучение собственной фундаментальной LLM, замена GitHub/GitLab как полноценного Git-хостинга.

## 3. Пользователи и права

| Роль | Разрешения |
| --- | --- |
| Owner/Admin | Политики, проекты, провайдеры, секреты, |
|   | исполнители, аудит |
| Maintainer | Управление задачами и подтверждение |
|   | PR/релизов в пределах проекта |
| Developer | Создание задач, запуск анализов, просмотр |
|   | результатов, предложение изменений |
| Auditor | Чтение отчётов, журналов и политик без права |
|   | изменения |
| Agent | Ограниченные краткоживущие полномочия по |
|   | конкретной задаче |

RBAC применяется сервером, а не только интерфейсом. Межпроектная изоляция обязательна; каждая операция записывается с actor, project_id, run_id, timestamp, outcome.


## 4. Архитектура

- CLI: Python/Typer; REST API: FastAPI; Web UI: React (=5>1O70B5;5= 4;O CLI MVP).

- Core Orchestrator: :>=5G=K9 02B><0B 7040G, planner, router, policy engine, context manager, checkpoint manager, result aggregator.

- Executors: Local (>3@0=8G5==K9), Docker sandbox, GitHub Actions adapter; >48= 8=B5@D59A исполнения.

- LLM adapters: OpenAI, Anthropic, Ollama/vLLM; configurable models, token limits, fallback 8 отключаемый AI.

- Persistence: SQLite 4;O single-user, PostgreSQL 4;O multi-user; 0@B5D0:BK =0 ;>:0;L=>< 48A:5 8;8 MinIO; Redis опционально для очереди.

- Git adapters: ;>:0;L=K9 Git 8 GitHub API; 8=B53@0F88 4>;6=K 1KBL 70<5=O5<K<8.

- Security tools: Semgrep, Gitleaks, Trivy/OSV, linters, B5AB>2K5 D@59<2>@:8 8 A1>@:0; 70?CA:0BL реальные бинарные инструменты.

Состояния: CREATED → DISCOVERY → PLANNED → AWAITING_APPROVAL → RUNNING → VERIFYING → COMPLETED; дополнительные FAILED, CANCELLED, PAUSED. Все переходы идемпотентны и журналируются. После рестарта выполнение продолжается с последнего подтверждённого checkpoint без повторного применения изменений.

- Task Planner AB@>8B DAG H03>2 A 7028A8<>ABO<8, :@8B5@8O<8 CA?5E0 8 1N465B><.

- Agent Router =07=0G05B 035=B0 ?> capability, 4>ABC?C, AB>8<>AB8 8 4>ABC?=>AB8.

- Context Manager 8=45:A8@C5B @5?>78B>@89 8 ?5@540QB <8=8<0;L=K9 @5;520=B=K9 :>=B5:AB, исключая секреты.

- Executor >3@0=8G8205B CPU/RAM, 2@5<O, A5BL, D09;>2CN A8AB5<C 8 G8A;> ?>2B>@>2; :><0=4K allowlist/policy-based.

- Result Aggregator D>@<8@C5B 548=K9 8B>3 8 AAK;:8 =0 4>:070B5;LAB20; AI-2K2>4 =5 <>65B заменить статус реального теста.

- Human-in-the-loop: approval ?5@54 87<5=5=85< :@8B8G=KE D09;>2, C40;5=85<, <83@0F859 --, merge и deployment.

## 5. Оркестратор — обязательный модуль P0

## 6. AI-агенты и контракты

| Агент | Вход | Выход |
| --- | --- | --- |
| Requirements | Описание идеи, ограничения | ТЗ, user stories, acceptance |
|   |   | criteria |
| Architect | ТЗ, текущее состояние | ADR, схема модулей, API/DB |
|   |   | проект |
| Planner | ТЗ, ADR | Backlog, зависимости, оценка |
| Bootstrap | Утверждённый план | Каркас, CI, README, |
|   |   | конфигурация |
| Developer | Issue, контекст | Patch/commit, пояснение, |
|   |   | тесты |
| Reviewer | Diff, стандарты | Замечания с file/line/severity |
| Security | Diff, scanner reports | Уязвимости, evidence, |
|   |   | рекомендации |
| QA | Требования, код | Тест-кейсы, результаты, |
|   |   | coverage |
| Database | Schema, queries | Анализ миграций, индексов, |
|   |   | транзакций |
| Performance | Метрики, профили | Bottlenecks и воспроизводимые |
|   |   | бенчмарки |


| DevOps | Сборка, окружения | CI/CD, release plan, rollback |
| --- | --- | --- |
| Maintenance | Issues, telemetry | План исправлений, |
|   |   | проверяемые PR |

Контракт каждого агента: task_id, input_refs, allowed_tools, scope, max_cost, max_duration, output_schema, evidence_refs, status, confidence. Выход валидируется JSON Schema; неверный ответ не считается выполненной задачей.

## 7. Функциональные требования (FR)

| ID | Требование |
| --- | --- |
| FR-01 | Импорт локальной папки/Git URL/GitHub repo; |
|   | проверка доступа и состояния Git. |
| FR-02 | Определение языка/стека: Laravel/PHP, |
|   | React/TS, Python, Go, Rust; ручное |
|   | переопределение. |
| FR-03 | CREATE: диалог требований → версия ТЗ → |
|   | approval → ADR → backlog → bootstrap. |
| FR-04 | DEVELOP: Issue → план → ветка → patch → тесты |
|   | → review → PR. |
| FR-05 | MODERNIZE: baseline, impact analysis, малые PR, |
|   | миграционные планы и rollback. |
| FR-06 | AUDIT: read-only режим по умолчанию, находки |
|   | с severity и evidence. |
| FR-07 | MAINTAIN: triage дефектов, dependency updates, |
|   | регрессионные тесты. |
| FR-08 | Реестр агентов, capability routing, |
|   | последовательные и параллельные шаги. |
| FR-09 | Возобновление задач, retry с backoff, отмена и |
|   | тайм-аут. |
| FR-10 | Отчёты JSON/Markdown/SARIF/JUnit; |
|   | комментарии к PR с защитой от дублей. |
| FR-11 | Policy-as-code и блокирующие Quality Gates, |
|   | независимые от AI. |
| FR-12 | Локальная работа без GitHub и offline с |
|   | локальной моделью при наличии. |
| FR-13 | Self-Hosted многопроектный режим с RBAC, API |
|   | и очередями. |
| FR-14 | Подключение GitHub Actions reusable workflows |
|   | через workflow_call. |
| FR-15 | Настройка бюджета токенов/стоимости, |
|   | лимитов и маршрутизации моделей. |
| FR-16 | Протоколирование решений, команд, версий |
|   | инструментов и артефактов. |
| FR-17 | Управляемое создание PR; запрет auto-merge и |
|   | production без approval. |
| FR-18 | Маскировка секретов и фильтрация контекста |
|   | перед вызовом внешних LLM. |

## 8. Нефункциональные требования (NFR)

| ID | Критерий |
| --- | --- |


| NFR-01 | CLI поддерживает Linux, Windows, macOS; |
| --- | --- |
|   | контейнерный сервер — Linux x86_64. |
| NFR-02 | Не менее 10 параллельных заданий на сервере |
|   | при выделенных ресурсах; лимит |
|   | конфигурируемый. |
| NFR-03 | Сбой исполнителя не теряет подтверждённые |
|   | состояния и артефакты. |
| NFR-04 | Для API и UI: аутентификация, RBAC, TLS, rate |
|   | limiting, audit trail. |
| NFR-05 | Результаты проверок воспроизводимы на |
|   | закреплённых версиях инструментов. |
| NFR-06 | Модульные тесты core ≥80% покрытия; |
|   | критические state transitions — 100% ветвей по |
|   | тест-кейсам. |
| NFR-07 | Логи без секретов, структурированные метрики |
|   | и trace/run_id. |
| NFR-08 | Пользователь может экспортировать данные |
|   | проекта и удалить его по политике хранения. |

## 9. GitHub Actions

Reusable workflows: quality, security, test-php, test-node, test-python, test-go, test-rust, ai-review, release. Caller workflows используют workflow_call и минимальные permissions. Версии сторонних Actions закреплять на SHA; при доступе к секретам использовать environments и approval. Для fork PR — непривилегированный read-only workflow; не запускать недоверенный код с pull_request_target и write-token. Self-hosted runners использовать только с доверенными заданиями и изоляцией.

## 10. Quality Gates и правила принятия

| Gate | Блокирует merge при |
| --- | --- |
| Build | Ошибка сборки или установки зависимостей |
| Tests | Падение обязательных тестов |
| Static | Ошибки линтера/типизации выше заданного |
|   | порога |
| Security | Подтверждённые Critical/High по политике и |
|   | новые секреты |
| Coverage | Снижение покрытия критичных модулей ниже |
|   | порога |
| Review | Отсутствие требуемого человеческого approval |

AI Review носит рекомендательный характер; блокировка возможна только при явной утверждённой политике и проверяемых результатах. Baseline существующих проблем допускается с исключениями, владельцем и сроком действия.

## 11. Конфигурация проекта

Файл .ai-coding.yml: schema_version, project, mode, stacks, execution.backend, ai.provider/model, agents.enabled, security.network, quality_gates, budgets, approvals, artifacts.retention. Конфигурация проходит schema validation. Секреты передаются только через secret store / environment, никогда в

YAML.


## 12. Безопасность и модель угроз

- 854>25@5==K5 README, issues, diff, 7028A8<>AB8 8 B5AB>2K5 40==K5 @0AA<0B@820NBAO :0: возможные prompt injection.

- ‐@020 035=B0< 2K40NBAO ?> ?@8=F8?C least privilege; shell/network/file write 70?@5I5=K ?> умолчанию до назначения роли.

- ‑A?>;=5=85 :>40 ‑ 2 87>;8@>20==KE :>=B59=5@0E 157 privileged, 157 Docker socket, A read-only mounts там, где возможно.

- =5H=85 LLM 8A?>;L7CNBAO B>;L:> ?>A;5 ?@>25@:8 ?>;8B8:8 >B?@02:8 :>40; @568< local-only полностью запрещает egress.

- !5:@5BK =5 2:;NG0BL 2 ;>38, prompts, PR-:><<5=B0@88 8 0@B5D0:BK; @50;87>20BL redaction 8 секрет-сканирование.

- >4?8A0==K5/?@>25@5==K5 0@B5D0:BK, SBOM, provenance, :>=B@>;L F5?>G:8 ?>AB02>: 8 6C@=0; изменений.

- #40;5=85/<83@0F88/45?;>9 ‐ B>;L:> ?> approval; rollback 8 backup >1O70B5;L=K 4;O рискованных операций.

Основные сущности: users, projects, repositories, runs, tasks, task_dependencies, agents, agent_executions, approvals, artifacts, findings, policies, model_usage, audit_events, checkpoints. Идентификаторы UUID, время UTC, optimistic concurrency/version для состояния задач.

REST API: POST /projects; POST /projects/{id}/runs; GET /runs/{id}; POST /runs/{id}/approve; POST /runs/{id}/cancel; GET /runs/{id}/artifacts; GET /projects/{id}/findings; GET /audit-events. Операции создания идемпотентны по Idempotency-Key.

Команды: ai-coding init, project add, create, develop, modernize, audit, maintain, run status, run resume, run cancel, config validate, agents list, report export. Для изменяющих операций поддерживать --dry- run и --require-approval.

## 13. API и модель данных

## 14. CLI интерфейс

## 15. Развёртывание

| Профиль | Компоненты |
| --- | --- |
| Local Lite | CLI + SQLite + Git worktree + локальный |
|   | sandbox; LLM опционально |
| Self-Hosted | FastAPI + worker + PostgreSQL + artifact store + |
|   | Web UI + Docker executor |
| GitHub Hybrid | Self-Hosted или CLI + GitHub App/token + |
|   | reusable workflows + GitHub Rulesets |

GitHub Actions не является автономным offline-оркестратором; offline режим должен выполняться через локальный scheduler/executor. В многопользовательском режиме обязательны изоляция проектов, резервное копирование БД и контролируемая ротация ключей.

## 16. Этапы разработки и зависимости

| Спринт | Содержание | Результат/приёмка |
| --- | --- | --- |
| S1 — 2 нед. | Архитектура, ADR, схемы, CLI, | init/config validate; тесты |
|   | config schema, Git abstraction | каркаса |
| S2 — 2 нед. | Orchestrator FSM, persistence, | Восстановление после |
|   | checkpoint, retry, policy | прерывания; unit tests |


| S3 — 2 нед. | Local/Docker executor, sandbox, | Безопасный локальный audit |
| --- | --- | --- |
|   | tool registry |   |
| S4 — 2 нед. | GitHub adapter, reusable | CI проходит на пилотном |
|   | workflows, PR/report integration | репозитории |
| S5 — 2 нед. | CREATE: requirements, architect, | Новый проект из задачи до PR |
|   | planning, bootstrap |   |
| S6 — 2 нед. | DEVELOP и MODERNIZE, AI | Функция/рефакторинг через |
|   | review/fix, approvals | отдельный PR |
| S7 — 2 нед. | AUDIT/MAINTAIN, security tools, | Воспроизводимые результаты |
|   | reports, QA | сканеров |
| S8 — 2 нед. | Self-Hosted API, RBAC, Web UI, | Два проекта и несколько |
|   | observability | пользователей |
| S9 — 2 нед. | Hardening, load/security tests, | Приёмочные сценарии и v2.1 |
|   | docs, release |   |

Оценка 18 недель для небольшой команды — плановый ориентир, не гарантия; возможен выпуск промежуточного Local MVP после S3–S4.

## 17. Приёмочные сценарии

- AC-01: !>740BL =>2K9 ?@>5:B 87 B5:AB>2>3> >?8A0=8O; ?>;CG8BL A>3;0A>20==>5 "-, ADR, backlog, bootstrap и PR.

- AC-02: ->4:;NG8BL ACI5AB2CNI89 Laravel+React ?@>5:B; 2K?>;=8BL read-only audit 8 ?>;CG8BL подтверждённые находки с путями файлов.

- AC-03: -> Issue 4>1028BL DC=:F8N 2 >B45;L=>9 25B:5, 2K?>;=8BL B5ABK, AD>@<8@>20BL PR 157 прямого push в main.

- AC-04: !<>45;8@>20BL ?045=85 worker 8 C1548BLAO, GB> run 2>7>1=>2;O5BAO 157 4C1;8@>20=8O коммитов.

- AC-05: ->4:;NG8BL ;>:0;L=CN <>45;L 8 2K?>;=8BL audit 157 2=5H=8E A5B52KE 70?@>A>2.

- AC-06: #O728<K9 PR =5 ?@>E>48B Quality Gate; G5;>25: 2848B ?@8G8=K 8 4>:070B5;LAB20.

- AC-07: ->?KB:0 prompt injection 87 README =5 40QB 035=BC 4>?>;=8B5;L=KE ?@02.

- AC-08: 70?CA: =0 self-hosted A5@25@5 A 42C<O ?@>5:B0<8 ?>4B25@6405B RBAC 8 >BACBAB285 CB5G:8 контекста.

- AC-09: ->4:;NG8BL GitHub Actions : 42C< @07=K< AB5:0<, ?>;CG8BL AB0BCAK ?@>25@>: 2 PR.

- AC-10: -@>25@8BL dry-run, @CG=>5 approval, >B<5=C 8 M:A?>@B audit trail.

## 18. Риски и меры

| Риск | Митигирующая мера |
| --- | --- |
| Prompt injection | Недоверенный контент, tool policy, изоляция, |
|   | approvals |
| Утечка исходного кода | Local-only policy, redaction, provider allowlist |
| Повреждение репозитория | Git worktree, отдельная ветка, diff review, |
|   | rollback |
| Нестабильность LLM | Schema validation, deterministic gates, retry |
|   | budget |
| Высокие расходы | Token caps, caching, routing, usage reports |
| Долгие CI задания | Параллелизм, incremental scans, timeouts |
| Уязвимые runners | Эфемерные runner, разделение trust zones |


## 19. Приоритетный backlog P0

- P0-01: !>740BL @5?>78B>@89, CI 8 :>4AB09;.

- P0-02: CLI 8 :>=D83C@0F8>==0O AE5<0.

- P0-03: State machine + SQLite persistence.

- P0-04: Sandbox executor 8 Git worktree.

- P0-05: Policy/approval engine.

- P0-06: GitHub reusable workflows + checks.

- P0-07: Agent contracts 8 provider adapters.

- P0-08: Audit tools + >BGQBK JSON/Markdown.

- P0-09: DEVELOP end-to-end G5@57 PR.

- P0-10: CREATE <8=8<0;L=K9 end-to-end.

Функция считается завершённой только при наличии кода, тестов, обновлённой документации, безопасных значений по умолчанию, прохождения CI, проверенного сценария отказа (если применимо), отчёта и одобрения ответственного. Выпуск v2.1 допускается после прохождения всех AC-01…AC-10, закрытия блокирующих дефектов и подготовки эксплуатационной инструкции.

Конец документа. Изменения требований оформлять через ADR/Change Request с оценкой влияния на сроки и безопасность.

## 20. Определение готовности (Definition of Done)
