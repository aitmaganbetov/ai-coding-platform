# Отчёт Sprint S1 — Архитектура, ADR, схемы, CLI, config schema, Git abstraction

Дата: 2026-11-05 · Ветка: `claude/cool-sagan-ho737s` · Приёмка ТЗ §16: «init/config validate; тесты каркаса».

## 1. Результат

| Пункт задания | Результат |
| --- | --- |
| Изучить ТЗ | Выполнено по `.md`, затем сверено с PDF (совпадает) — [tz-notes.md](../tz-notes.md) |
| Проверить репозиторий | Был только ТЗ (.md) и LICENSE (GPL-3.0); позже добавлен ТЗ в PDF; ничего не удалялось |
| Архитектура платформы | [architecture/overview.md](../architecture/overview.md) |
| Модули и зависимости | [architecture/modules.md](../architecture/modules.md), слои закреплены import-linter |
| ADR | 15 ADR — [adr/README.md](../adr/README.md) |
| Структура каталогов | [modules.md](../architecture/modules.md#структура-репозитория) |
| `pyproject.toml` | Зависимости с диапазонами + `uv.lock`, ruff, mypy strict, pytest, coverage ≥80%, import-linter |
| Базовый пакет | `src/ai_coding` (domain, security, contracts, detection, git, config, cli) |
| CLI | `ai-coding --help`, `--version`, `init`, `config validate`, `config schema` |
| JSON Schema `.ai-coding.yml` | `src/ai_coding/schemas/ai-coding.schema.json` (Draft 2020-12, генерируется, drift-check) |
| Unit Tests | 178 тестов (unit + integration с реальным git) |
| GitHub Actions | `ci.yml`, `repo-security.yml`, `dependabot.yml` |
| Документация | installation, development, configuration, cli, SECURITY, CONTRIBUTING |
| Backlog | [backlog.md](../backlog.md) — B-001…B-806, трассировка FR/NFR/AC |

## 2. Реализованные компоненты

- **domain** — перечисления ТЗ: Mode, RunState (10 состояний), Role, Stack, ExecutionBackend, ProviderName (с признаком локальности), NetworkPolicy, AgentName (12), ApprovalOperation (с неотключаемыми delete/migration/merge/deploy), Severity, GateResult.
- **security** — `Redactor` (OpenAI/Anthropic/GitHub/AWS/Slack/Google ключи, JWT, PEM, креды в URL, присваивания, литералы из окружения); детектор секретов в документах конфигурации.
- **config** — Pydantic-модели всех разделов ТЗ §11 с безопасными умолчаниями и межсекционными правилами; безопасный YAML-загрузчик (дубли ключей, лимит размера, UTF-8); 4-этапный валидатор; генератор шаблона.
- **contracts** — `AgentTask`/`AgentResult`/`AgentScope`, проверка `output_schema`, `validate_agent_output` (невалидный вывод ≠ выполненная задача).
- **detection** — Laravel/PHP, React/TypeScript/JavaScript, Python, Go, Rust; монорепозитории (1 уровень), пропуск vendor/node_modules, устойчивость к битым манифестам.
- **git** — порт `GitRepository`, адаптер `LocalGitRepository`: status (porcelain v1 -z, переименования), ветки, remote, worktree add/list/remove, commit; защищённые ветки; валидация ref-имён; отключение hooks/fsmonitor.
- **cli** — Typer-приложение; `init` с детекцией git/стеков, `--dry-run`, `--force` с подтверждением и backup, атомарной записью; `config validate` text/json с кодами 0/1/2; `config schema`.

## 3. Фактически запущенные проверки

Запущено локально (Linux, 2026-11-05):

| Проверка | Команда | Результат |
| --- | --- | --- |
| Lockfile | `uv lock --check` | OK |
| Lint | `uv run ruff check .` | All checks passed |
| Format | `uv run ruff format --check .` | 72 files already formatted |
| Типы | `uv run mypy` (strict) | Success: no issues found in 30 source files |
| Слои | `uv run lint-imports` | 2 kept, 0 broken |
| Schema drift | `ai-coding config schema` + `diff` | нет расхождений |
| Тесты, Python 3.13 | `uv run pytest --cov` | **178 passed**, покрытие **97.75%** (порог 80%) |
| Тесты, Python 3.12 | `uv run --python 3.12 --isolated pytest` | **178 passed** |
| Сборка | `uv build` | wheel + sdist; wheel содержит schema, шаблон, py.typed |
| Smoke | установка wheel в чистый venv → `--version`, `init`, `config validate` | OK |
| Dogfooding | `ai-coding config validate .ai-coding.yml` | valid |
| Секреты | gitleaks 8.30.1 `dir .` | no leaks found |
| Зависимости | pip-audit 2.10.1 по lockfile | No known vulnerabilities found |
| Workflows | actionlint 1.7.7, zizmor 1.32 (offline) | без замечаний |
| Проверка защиты git | ручной эксперимент: `core.fsmonitor=<script>` + `git status` | без защиты скрипт исполняется, с `-c core.fsmonitor=false` — нет; закреплено тестом |

Результаты CI на GitHub (Windows/macOS) — см. раздел 5.

## 4. Найденные проблемы

1. **ТЗ (`.md`): повреждённая кодировка** в §4, §12, §17, §19 и сбитые разделы §1, §5, §13, §14, §20. Текст был восстановлен; после появления `AI_Coding_Platform_v2_1_TZ.pdf` сверка показала полное совпадение, PDF принят основным источником, ссылки на разделы приведены к PDF — см. [tz-notes.md](../tz-notes.md).
2. **Решено:** по решению владельца ТЗ повреждённый `.md` удалён и перегенерирован из PDF; текст совпадает с PDF дословно.
3. **Безопасность git**: обычный `git status` в недоверенном репозитории исполняет `core.fsmonitor` — устранено в адаптере (ADR-0008).
4. **Gitleaks** находил синтетические токены в тестах — тестовые значения теперь собираются динамически, allowlist не потребовался.
5. **Конфликт имён**: reusable workflow `security.yml` из ТЗ §9 vs CI безопасности самого проекта — CI переименован в `repo-security.yml`.
6. **Требует действия владельца**: включить защиту `main` (Rulesets: PR обязателен, статусы CI обязательны, запрет force-push) — B-008; включить private vulnerability reporting.

## 5. CI на GitHub

После слияния PR #1 в `main` (коммит `0745155`) GitHub Actions выполнил все проверки — **успешно**:

| Workflow / job | Результат |
| --- | --- |
| CI · Lint & type-check (ruff, mypy, import-linter, schema drift, self-validate) | ✅ success |
| CI · Tests ubuntu-24.04 × py3.12 / py3.13 | ✅ success / ✅ success |
| CI · Tests windows-2025 × py3.12 / py3.13 | ✅ success / ✅ success |
| CI · Tests macos-15 × py3.12 / py3.13 | ✅ success / ✅ success |
| CI · Build & smoke-test wheel | ✅ success |
| Repository security · Gitleaks (вся история) + pip-audit | ✅ success |

Прогон CI: https://github.com/aitmaganbetov/ai-coding-platform/actions/runs/41975059069. Тот же набор прошёл и на PR (run 41974921663). Один прогон для merge-коммита отменён штатно (`concurrency: cancel-in-progress`) последующим push.

## 6. Незавершённые задачи S1

- B-008 — настройка Rulesets в GitHub (нужны права администратора репозитория).
- B-034/B-035/B-036 (импорт по Git URL, diff/patch API, commit trailers) перенесены в S2/S3, где появляются использующие их sandbox и оркестратор; в S1 они были бы неиспользуемым кодом.

## 7. План Sprint S2 (Orchestrator FSM, persistence, checkpoint, retry, policy)

Цель приёмки: «Восстановление после прерывания; unit tests».

1. **Persistence (E05)**: SQLAlchemy 2 + Alembic, все сущности ТЗ §13, SQLite WAL, optimistic `version`, append-only `audit_events`, локальный `ArtifactStore` с SHA-256.
2. **State Manager (B-110)**: таблица переходов RunState, идемпотентные переходы с ключом, журнал событий; 100% ветвей переходов тестами.
3. **Task Planner + Workflow Executor (B-111, B-112)**: DAG с проверкой циклов, параллельное исполнение с `max_parallel_tasks`.
4. **Checkpoint + Retry & Recovery (B-113…B-115)**: checkpoint после верификации шага, backoff+jitter, отмена, таймаут; тест «kill -9 worker → resume без дублирования эффектов» (AC-04).
5. **Policy Engine + Approval Manager (B-130…B-133)**: решения policy-as-code, approval через CLI, `--dry-run`, `--require-approval`.
6. **LLM-порт и бюджеты (B-140…B-142)** без конкретных провайдеров.
7. **Audit Logger + structlog** с redaction (B-120); CLI `run status|resume|cancel` (B-121).
8. Зависимости S2: SQLAlchemy, Alembic, structlog (ADR-0002).
