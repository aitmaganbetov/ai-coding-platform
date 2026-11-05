# Отчёт Sprint S4 — GitHub adapter, reusable workflows, PR/report integration

Дата: 2026-11-05 · Ветка: `claude/cool-sagan-ho737s` · Приёмка ТЗ §16: «CI проходит на пилотном репозитории».

## 1. Результат приёмки

| Критерий | Чем подтверждается | Результат |
| --- | --- | --- |
| **CI проходит на пилотном репозитории** (B-313, AC-09 — два стека) | `.github/workflows/pilots.yml`: пилоты `examples/pilot-python` (uv, pytest, ruff) и `examples/pilot-node` (`node --test`, gitleaks, semgrep) через **те же reusable workflows**, которые получит внешний пользователь | см. §3 (CI этого PR) |
| PR только под политикой, без merge и auto-merge (FR-17, AC-03) | `ai-coding pr open` / `pr resume`: approval `pr_create`, egress-политика, запрет push в protected, проверка head SHA, base и auto-merge | ✅ `tests/integration/test_github.py` |
| Идемпотентность (AC-04) | Повторный прогон или resume не создаёт второй push, PR или комментарий | ✅ `test_pr_workflow_*`, `test_cli_pr_open`, `test_cli_comment_check_protection` |
| Отчёты в PR (FR-10, AC-06) | PR-комментарий без дублей (маркер + upsert); check run с аннотациями по файлам и строкам (пачки по 50) | ✅ на fake API; на реальном GitHub — в CI пилотов |
| SARIF 2.1.0, JUnit, `report export` (B-320, B-321) | SARIF загружается в Code Scanning (`security.yml`); JUnit выгружается артефактом | ✅ `test_report_exports.py`; загрузка — в CI |
| Безопасность workflows (§9) | `permissions: {}`, SHA-pinning, нет template injection, fork PR без записей, нет `pull_request_target`; actionlint и zizmor в CI | ✅ `test_workflow_files.py` + job `workflows` |

## 2. Реализованные компоненты

| Компонент | Модуль | Ключевое |
| --- | --- | --- |
| Порт `RepositoryHost` | `integrations/host.py` | Protocol; merge, auto-merge и force-push в нём намеренно отсутствуют |
| HTTP-клиент GitHub | `integrations/github/client.py` | httpx, только https, retries и backoff, rate limit, пагинация, redaction; `download` по подписанному redirect без токена, с лимитом |
| Аутентификация | `integrations/github/auth.py` | fine-grained token; GitHub App (JWT RS256 → installation token, кэш) |
| Адаптер GitHub | `integrations/github/host.py` | идемпотентные PR, upsert комментариев, check runs и statuses, rulesets, workflow dispatch, runs, jobs, cancel, артефакты с безопасной распаковкой |
| GitHubActionsExecutor | `integrations/github/executor.py` | allow-list workflow, `correlation_id` в `run-name`, отмена по таймауту, сводка jobs и артефактов, скачивание |
| Git push | `git/local.py` | без force и hooks; токен — через `GIT_CONFIG_*`; проверка URL; `remote_branch_sha` |
| Workflow PR | `workflows/pull_request.py` | DAG discover → push → PR (approval) → verify; журнал эффектов с пробами |
| Отчёты | `reports/sarif.py`, `reports/junit.py` | SARIF 2.1.0 (fingerprints, baselineState, security-severity); JUnit |
| CLI | `report export`, `github comment/check/protection`, `pr open/resume` | `run resume` отказывается продолжать PR-прогон, для которого нет обработчиков |
| Reusable workflows | `.github/workflows/{quality,security,test-python,test-node,test-php,test-go,test-rust,release}.yml` | `workflow_call`, минимальные права, SHA-pinning, env вместо шаблонов, gating для форков |
| Пилоты | `examples/`, `pilots.yml` | два стека через reusable workflows |
| Lint workflow-файлов в CI | job `workflows` в `repo-security.yml`, `.github/zizmor.yml` | actionlint 1.7.7, zizmor 1.32.0 |
| Тестовый двойник | `tests/fake_github.py` | настоящий HTTP на localhost; формы ответов по REST-документации |

Новые ADR: [0021](../adr/0021-github-integration.md), [0022](../adr/0022-reusable-workflows.md). Руководство: [github.md](../github.md).

## 3. Фактически запущенные проверки

Локально: Linux, Docker 29.9.2, 2026-11-05.

| Проверка | Результат |
| --- | --- |
| `pytest --cov`, Python 3.13 (gitleaks в PATH, Docker) | **631 passed, 0 skipped**, покрытие 96% |
| `pytest`, Python 3.12 | **631 passed, 0 skipped** |
| ruff, ruff format, mypy strict (103 файла), import-linter | без замечаний |
| actionlint; zizmor `--offline` с конфигом | без замечаний |
| gitleaks `dir` и `git` | no leaks found |
| `uv lock --check`; сборка wheel и `ai-coding --help` из wheel | OK; команды `report`, `github`, `pr` присутствуют |
| Локальная симуляция пилотов | node security audit (Docker) PASS; python ruff audit PASS; pytest пилота 2 passed |

Результаты CI (Linux/Windows/macOS × 3.12/3.13, sandbox, pilots, workflow lint) — в PR.

## 4. Найденные и исправленные проблемы

1. **Template injection в собственном workflow.** zizmor нашёл `${{ steps.audit.outputs.verdict }}` в `run:`; значение теперь передаётся через `env`. Тест запрещает такие выражения во всех workflow.
2. **Ложное срабатывание guardrail-теста.** Упоминание `pull_request_target` в комментарии `ci.yml` проваливало тест. Тест теперь проверяет разобранные триггеры, а не текст.
3. **Generic resume проваливал PR-прогон.** `run approve --resume` не знает PR-обработчиков: шаг падал с «no route», и прогон необратимо становился FAILED. Добавлены `pr resume` и отказ generic-resume с подсказкой. Сценарий покрыт тестом.
4. **Approval и egress.** Тест ожидал отказ egress-политики сразу, но approval `pr_create` запрашивается раньше, в состоянии PLANNED. Тест исправлен: после одобрения egress всё равно блокирует push, PR не создаётся.
5. **Rich съедал `[...]` в выводе `--dry-run`.** Вердикт `[require_approval]` воспринимался как markup и пропадал. Вывод экранируется.
6. **Конфиг ruff пилота.** Пилот наследовал строгий конфиг корня репозитория. Добавлен собственный `[tool.ruff]`.
7. **Тест App-аутентификации.** Замороженное «сейчас» оказалось позже срока токена. Время в тесте исправлено.
8. **Невыполненные пункты backlog, найденные при сверке перед отчётом:**
   - не было порта `RepositoryHost` (B-300);
   - не было артефактов в Actions executor (B-303);
   - не было actionlint и zizmor в CI (B-312).

   Все три реализованы в этом спринте, а не помечено выполненным заранее.

## 5. Ограничения и незавершённое

- **`ai-review.yml` перенесён в S6** (B-507): Reviewer Agent требует LLM-адаптеров S5. Заглушку не делаем.
- **GraphQL не используется:** REST покрывает все операции (ADR-0021).
- **Реальный GitHub в тестах.** Интеграционные тесты идут против локального двойника API. Реальный API проверяется только в CI пилотов: комментарий, check run, SARIF на этом PR. `GitHubActionsExecutor` на реальном GitHub пока не запускался, нужен workflow с `workflow_dispatch` (план S5).
- **Пилоты встроены в репозиторий**, а не вынесены в отдельные репозитории. Так проще воспроизводить AC-09. Внешний репозиторий подключается так же (`docs/github.md`).
- **Права `GITHUB_TOKEN` у вызывающего.** Если организация ограничивает default permissions, вызывающий workflow должен явно выдать `checks: write` и `pull-requests: write`.

## 6. План Sprint S5 — CREATE: requirements, architect, planning, bootstrap

1. LLM-порт и адаптеры OpenAI, Anthropic, Ollama и vLLM: бюджеты, маршрутизация, redaction prompts (ADR-0009).
2. Агенты Requirements, Architect, Planner, Bootstrap с контрактами JSON Schema (ADR-0011).
3. Режим CREATE: требования → архитектура и ADR → план → скелет проекта → PR через `pr` workflow S4.
4. `ai-coding create`, `ai-coding agents list`.
5. Workflow с `workflow_dispatch` для `GitHubActionsExecutor` и e2e-проверка на GitHub.
