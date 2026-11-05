# Отчёт Sprint S6 — DEVELOP и MODERNIZE, AI review/fix, approvals

Дата: 2026-11-05 · Ветка: `claude/cool-sagan-ho737s` · Приёмка ТЗ §16: «Функция/рефакторинг через отдельный PR» (AC-03, P0-09).

## 1. Результат приёмки

| Критерий | Чем подтверждается | Результат |
| --- | --- | --- |
| **AC-03:** по Issue добавить функцию в отдельной ветке, выполнить тесты, сформировать PR без прямого push в main | Сценарий `test_develop_opens_pr_with_report_body` и CLI-тест `test_develop_issue_with_pr_interactive`: issue #7 из GitHub API → изменения с тестами → тесты → review → ветка `ai/7-…` → проверка → push → PR (`Closes #7`, без auto-merge). `main` не меняется | ✅ на fake LLM API и fake GitHub по HTTP, настоящий git и remote |
| **Тесты реально выполняются** | `test_develop_fix_loop_with_real_sandbox_tests`. **Настоящий Docker** (Node, без сети): итерация 1 падает (`# fail 1`), вывод возвращается агенту, итерация 2 проходит (`# pass 2`). Затем проверка на дереве коммита | ✅ |
| FR-04: Issue → план → ветка → patch → тесты → review → PR | `workflows/develop.py`, `ai-coding develop` | ✅ |
| B-503: исправление с лимитом итераций и бюджета | Упавшие тесты и critical/high замечания запускают следующую итерацию. После `max_iterations` прогон FAILED с причиной. Бюджет проверяет guard | ✅ `test_review_blocking_finding_drives_another_iteration`, `test_failing_tests_after_max_iterations_fail_the_run` |
| FR-05: baseline, анализ влияния, малые PR, план миграции, rollback | Красный baseline останавливает прогон. План утверждает человек. Шаг = ветка = PR без регрессий. `--continue` выполняет следующий шаг. Откат влитого шага — `git revert` в отдельной ветке | ✅ `tests/integration/test_modernize.py` (7), CLI-тест |
| ТЗ §10: AI review рекомендателен | Check run `neutral`; блокирует только `ai_review_blocking: true` | ✅ `test_advisory_review_…`, `test_review_policy_blocking_and_skip` |
| ТЗ §12: код агента — только в изоляции | Копия дерева, `--network none`, read-only root, без capabilities, не от root. Песочница без сети подтверждена тестом | ✅ `test_python_unittest_in_sandbox_without_network` |
| Approvals | Удаление → `delete`; CI и конфигурация → `critical_file_change`; план → `spec_approval`; PR → `pr_create`. Пауза на approval больше не тратит попытку | ✅ `test_critical_files_and_deletions_need_approval`, `test_approval_pause_does_not_consume_attempts` |

## 2. Реализованные компоненты

| Компонент | Модуль | Ключевое |
| --- | --- | --- |
| Developer Agent (B-500) | `agents/developer.py` | Полное содержимое файлов вместе с тестами. Проверки путей, create/modify/delete и обязательности тестов |
| Reviewer Agent (B-501) | `agents/reviewer.py` | file/line/severity. Строки проверяются по числу строк новой версии файла. Вердикт согласован с замечаниями |
| Database Agent (B-505) | `agents/database.py` | Запускается, если затронуты миграции. Оценивает обратимость, блокировки, индексы, транзакции |
| Planner (модернизация) | `agents/planner.py` (`MigrationPlannerAgent`) | Анализ влияния и шаги с проверкой и откатом. Своя схема |
| Тесты в песочнице | `tools/testrun.py` | План тестов по стеку или из конфигурации. Установка зависимостей — единственный шаг с сетью и только по политике. Разбор результатов pytest, node и unittest |
| Цикл реализации | `workflows/implement.py` | Выгрузка базы → изменения → тесты → review/db → обратная связь. Артефакт на каждую итерацию. Approval удалений и критичных файлов. Один коммит в worktree |
| DEVELOP (B-502) | `workflows/develop.py` | `develop.implement` → `develop.verify` → PR. Описание PR из результата |
| MODERNIZE (B-504) | `workflows/modernize.py` | baseline → plan → approve → step → verify → PR; откат |
| AI review (B-507) | `workflows/review.py`, `ai-coding review`, `.github/workflows/ai-review.yml` | Diff base…head, комментарий и check run. Ключ в environment, форки исключены |
| CLI (B-506) | `ai-coding develop`, `ai-coding modernize`, `ai-coding review`, `cli/approvals.py` | Интерактивные approvals; `--resume`, `--dry-run`, `--require-approval`, `--pr` |
| Общий запуск агентов | `workflows/agent_runtime.py` | Вынесено из CREATE: политика агента, LLM, approval внешнего провайдера, перевод ошибок |
| Git | `git/local.py` | `export` (`git archive`), `revert` (с `--no-commit`), `is_merge`, `commits_with_trailer` |
| GitHub | `GitHubHost.get_issue`, порт `RepositoryHost` | Issue как недоверенный вход агента |
| Движок | `orchestrator/engine.py` | Пауза на approval или по бюджету не тратит попытку |
| Конфигурация | `quality_gates.tests.{command, install, install_hosts, image, timeout_seconds}` | Схема перегенерирована |
| Образы рантаймов | `python-runtime`, `node-runtime` | `mirror.gcr.io` по digest; `ai-coding tools pull` / `list` |

Новые ADR: [0025](../adr/0025-develop-loop-sandbox-tests-review.md), [0026](../adr/0026-modernize-and-rollback.md). Руководство: [develop.md](../develop.md). Обновлены cli, configuration, github, backlog, архитектура, README.

## 3. Фактически запущенные проверки (Linux, Docker, 2026-11-05)

| Проверка | Результат |
| --- | --- |
| `pytest --cov`, Python 3.13 (Docker, образы python/node, gitleaks, node) | **731 passed, 0 skipped**, покрытие 95% |
| `pytest`, Python 3.12 | **731 passed, 0 skipped** |
| Новые тесты S6 | testrun 5, develop 11, modernize 7, CLI 6, агенты 9; движок 1; правила workflow +2 |
| ruff, ruff format, mypy strict (131 файл), import-linter | без замечаний |
| Дрейф схемы конфигурации; `uv lock --check` | нет / OK (новых зависимостей нет) |
| actionlint; zizmor `--offline` | без замечаний |
| gitleaks `dir` | no leaks found |
| Wheel | 8 схем агентов; команды `develop`, `modernize`, `review` доступны |

CI на GitHub (PR #8, коммит `9941a88`): **20 из 20 проверок успешны**. Среди них:

- тесты на Linux, Windows и macOS × Python 3.12 и 3.13;
- job «Sandbox, tools & self-audit (Docker)»: образы рантаймов скачиваются из `mirror.gcr.io`, тесты в настоящем Docker выполняются без пропусков;
- workflow lint, gitleaks, pip-audit;
- пилоты.

Первый прогон (`20faf1b`) упал на трёх CLI-тестах S6. CI задаёт `FORCE_COLOR=1`, и Rich вставлял ANSI-коды внутрь проверяемых фраз. Тесты исправлены: вывод сравнивается без ANSI-кодов; ошибка воспроизведена и проверена локально с тем же значением переменной.

## 4. Найденные и исправленные проблемы

1. **Пауза расходовала попытку задачи** (перенесено из S5) — approval и бюджет. Исправлено в движке; два теста S2 закрепляли старое поведение и обновлены.
2. **Правило «покажи файл перед правкой» срабатывало на удаления.** Удалению содержимое не нужно; правило оставлено только для `modify`.
3. **Кэш ответов и цикл исправления.** Если diff не изменился, review закономерно повторяет ответ из кэша. Значит, итерация, которая ничего не меняет, не может пройти review. Это ожидаемое поведение; тест сценария исправлен.
4. **Rich «съедал» `[low]` и `[must]`** в выводе плана и требований (затронут и `create` из S5). Строки экранируются целиком.
5. **Коллизия ключа `plan`.** План миграции перезаписывался планом разработчика, из-за чего падал `--continue`. Ключ переименован в `migration_plan`.
6. **У `git revert` нет `--no-verify`.** Hooks отключаются через `core.hooksPath`.
7. **Первая версия отката писала служебный файл в репозиторий.** Заменено на `revert --no-commit` и один коммит с trailers.
8. **Docker Hub отвечал 429.** Образы рантаймов берутся из `mirror.gcr.io` по digest; содержимое образа от этого не меняется.
9. **Отсутствующий образ давал FAILED вместо SKIPPED.** Теперь SKIPPED с подсказкой `tools pull`.
10. **Таблица `tools list` стала шире** и перенесла текст в тесте S3. Тест проверяет строки, а не фразу целиком.
11. **Цикл верно отклонил мой собственный тестовый сценарий.** Тест удалял `src/sum.js`, который импортируют настоящие тесты, и тесты в Docker закономерно упали. Ошибка была в тесте, не в коде.

## 5. Ограничения и незавершённое

- **Реальные модели не проверялись:** нет ключей, а загрузка моделей заблокирована сетью среды. Все агенты проверены против fake-сервера в формате API провайдера.
- **`ai-review.yml` на GitHub не запускался.** Ссылка на несуществующий environment создаёт его в настройках репозитория автоматически, а это изменение ваших настроек без согласования. Workflow проверен actionlint, zizmor и guardrail-тестами, сама команда — интеграционными тестами. Для боевого запуска:
  - создайте environment `ai-review` с reviewers;
  - добавьте секрет;
  - подключите workflow.
- **Python и pytest без сети.** Тестам Python по умолчанию нужна установка pytest, а значит сеть (`docker.network: bridge` и разрешённые хосты). Офлайн можно задать `quality_gates.tests.command: "python -m unittest"`. Адаптеры стеков S7 расширят команды по умолчанию.
- **Нет stacked PR в MODERNIZE.** Шаги строятся от базовой ветки, поэтому следующий шаг логично запускать после merge предыдущего.
- **Контекст DEVELOP ограничен** 24 тыс. токенов и 15 файлами, плюс файлы из `--file`. Для больших изменений нужен более узкий issue.
- **Database Agent только рекомендует.** С quality gates он не связан.

## 6. План Sprint S7 — AUDIT/MAINTAIN, security tools, reports, QA

1. **Агенты:** Security, QA, Performance, DevOps, Maintenance (B-604…B-608).
2. **Адаптеры стеков и инструменты:** Trivy, OSV, PHPStan, ESLint, mypy, go vet, cargo clippy; гейты Build, Tests и Coverage по реальным результатам.
3. **AUDIT по стекам:** Laravel и React (AC-02).
4. **MAINTAIN:** план исправлений по issues, проверяемые PR.
5. **Отчёты:** учёт `.semgrepignore` / `.gitleaksignore` (B-225).
6. **Команды тестов по умолчанию** без сети через адаптеры стеков.
