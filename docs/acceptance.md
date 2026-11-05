# Приёмочные сценарии AC-01…AC-10

ТЗ §20: v2.1 выпускается только после прохождения всех AC-01…AC-10. Каждый сценарий — автоматический тест с маркером `@pytest.mark.acceptance("AC-NN")`. Плагин `tests/acceptance_plugin.py` строит матрицу и роняет прогон, если хотя бы один AC:
- не покрыт сценарием;
- упал;
- **пропущен** — пропуск не считается приёмкой.

## Запуск

```bash
uv run ai-coding tools pull
```

```bash
uv run pytest -m acceptance --acceptance-report=acceptance/acceptance.json
```

Результат — `acceptance/acceptance.json` и таблица `acceptance/acceptance.md`. Нужен Docker: AC-02, AC-03, AC-05, AC-06 и AC-09 работают в песочнице.

В CI матрица строится в job «Sandbox, tools & self-audit» при каждом push и PR. Она попадает в summary job'а и в артефакт `self-audit-report`.

## Сценарии

| AC | Требование ТЗ §17 | Как проверяется | Тест |
| --- | --- | --- | --- |
| AC-01 | Новый проект из описания: согласованное ТЗ, ADR, backlog, bootstrap, PR | CREATE с моделью через HTTP API провайдера (fake-сервер): версия ТЗ утверждается, ADR и backlog в репозитории, каркас в ветке, PR | `test_create.py::test_create_offline_with_local_model`, `::test_create_with_pull_request` |
| AC-02 | Laravel+React: read-only audit с подтверждёнными находками и путями | Пилот `tests/fixtures/pilot-laravel-react`, Docker без сети: 18 находок с путями, репозиторий не изменён | `test_pilot_audit.py::test_laravel_react_pilot_audit` |
| AC-03 | Функция по Issue в ветке, тесты, PR без push в main | DEVELOP: первая попытка не проходит тесты в песочнице, вторая проходит; один коммит в ветке, `main` не тронут; PR | `test_develop.py::test_develop_fix_loop_with_real_sandbox_tests`, `::test_develop_opens_pr_with_report_body` |
| AC-04 | Падение worker'а: продолжение без дублирования коммитов | Процесс убивается до и после коммита. Серверный worker убивается посреди прогона, другой продолжает с checkpoint | `test_recovery.py::test_worker_crash_resumes_without_duplicate_commits[*]`, `test_worker.py::test_killed_worker_process_run_resumes_from_checkpoint` |
| AC-05 | Локальная модель, audit без внешних запросов | `local-only`: gitleaks и Semgrep в песочнице, triage на локальной модели (Ollama API на 127.0.0.1). Записывается **каждое** соединение процесса — только loopback; каждый контейнер запущен с `--network none`; ключ не попадает в запрос к модели | `tests/acceptance/test_ac05_local_model_offline.py` |
| AC-06 | Уязвимый PR не проходит Quality Gate; видны причины и доказательства | Базовая ветка проходит гейты. PR добавляет shell-инъекцию и ключ AWS: гейт `security` — FAIL с причиной; находки с файлом и строкой; check run `failure` с аннотациями на строках; комментарий в PR с причиной и путями; секрет нигде не публикуется | `tests/acceptance/test_ac06_vulnerable_pr.py` |
| AC-07 | Prompt injection из README не даёт прав | 7 сценариев: README, issue, diff, код, данные сканеров; модель **выполняет** инъекцию, платформа не даёт прав | `test_prompt_injection.py` (7 тестов) |
| AC-08 | Self-hosted, два проекта: RBAC и отсутствие утечки | Пять пользователей, два проекта; 404 на всё чужое; роли; токен с областью проекта; экспорт; журнал | `test_ac08.py::test_two_projects_rbac_and_no_context_leak` |
| AC-09 | GitHub Actions на двух стеках: статусы в PR | Тест читает `pilots.yml` и reusable workflows и выполняет то же, что они: аудит в песочнице, check run с именем, комментарий с ключом — для Python и Node на одном коммите; при повторе комментарий обновляется | `tests/acceptance/test_ac09_two_stacks_checks.py` |
| AC-10 | Dry-run, ручное approval, отмена, экспорт audit trail | `pr open`: dry-run ничего не выполняет; прогон ждёт человека, ничего не публикуется до approve; отмена с подтверждением; экспорт журнала с actor, run_id и outcome | `tests/acceptance/test_ac10_operator_controls.py` |

## AC-09 вживую

Те же workflows работают на каждом PR этого репозитория. Пример — PR [aitmaganbetov/ai-coding-platform#10](https://github.com/aitmaganbetov/ai-coding-platform/pull/10) (S8): проверки `ai-coding / quality (pilot-python)` и `ai-coding / security (pilot-node)` получили статус success. Их отчёты опубликованы комментариями в PR.
