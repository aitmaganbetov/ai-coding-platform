# Отчёт Sprint S3 — Local/Docker executor, sandbox, tool registry

Дата: 2026-11-05 · Ветка: `claude/cool-sagan-ho737s` · Приёмка ТЗ §16: «Безопасный локальный audit».

## 1. Результат приёмки

| Критерий | Чем подтверждается | Результат |
| --- | --- | --- |
| Аудит выполняется локально, без GitHub и LLM | `ai-coding audit` на локальном и Docker-бэкендах (`tests/integration/test_audit.py`) | ✅ |
| **Безопасно:** недоверенный код не запускается на хосте | Policy Engine: на хосте только инструменты анализа, которые читают проект; Docker-песочница без сети, с корнем и проектом только для чтения, без capabilities, non-root | ✅ проверено внутри реального контейнера (`test_docker_executor.py`: `NO-NET`, `ROOT-RO`, `SRC-RO`, `CapEff=0`, uid 1000) |
| **Read-only** (FR-06) | Снимок дерева до сканов и проверка после; изменение → run FAILED | ✅ `test_integrity_violation_fails_run` |
| **Офлайн** (AC-05) | `--network none`, `--pull never`, офлайн-правила Semgrep, отключённые телеметрия и проверка обновлений | ✅ аудит и тесты проходят без сети после `tools pull` |
| Находки с severity и evidence, пути файлов (AC-02, основа) | Аудит заведомо уязвимого проекта находит AWS-ключ, `shell=True`, `eval` с корректными путями и строками; секрет в отчёт не попадает | ✅ `test_docker_audit_finds_planted_issues_offline` |
| Детерминированные Quality Gates PASS/FAIL/SKIPPED | Движок гейтов, fail-closed, strict, baseline с владельцем и сроком | ✅ `test_quality.py`, `test_baseline_adoption_flow` |
| Воспроизводимость (NFR-05) | Образы по digest, версии инструментов в отчёте | ✅ gitleaks 8.30.1, semgrep 1.183.0, ruff 0.16.9 |

## 2. Реализованные компоненты

| Компонент | Модуль | Ключевое |
| --- | --- | --- |
| Порт Executor | `executors/base.py` | `CommandSpec` (только argv, без shell), `ExecutionResult`, `Mount` |
| Local Executor | `executors/local.py`, `process.py` | allow-list бинарников, env allow-list, rlimits, убийство дерева процессов, ограничение вывода и redaction |
| Docker Sandbox | `executors/docker.py` | флаги изоляции, образы только по digest, запрет Docker socket, `--pull never`, `tools pull` |
| Реестр инструментов | `tools/` | `ToolAdapter`, `Finding` с fingerprint, `ToolRunner` (сопоставление путей хоста и песочницы, версии, fail-closed) |
| Адаптеры | `tools/gitleaks.py`, `semgrep.py`, `ruff.py` | разбор JSON-вывода, severity, относительные пути |
| Офлайн-правила | `tools/rules/semgrep/ai-coding-baseline.yml` | 13 своих правил для Python, JS/TS/React, PHP/Laravel, Go, Rust; каждое проверено фикстурой |
| Quality Gates | `quality/gates.py` | 6 гейтов ТЗ §10, причины, evidence, strict |
| Baseline | `quality/baseline.py` | owner, reason, expires_at; истёкшие записи снова учитываются |
| Отчёты | `reports/audit.py` | JSON (schema_version 1) и Markdown; redaction |
| Workflow AUDIT | `workflows/audit.py` | DAG: discovery + снимок → сканы → гейты + целостность → отчёт; находки в таблице `findings` |
| Policy | `orchestrator/policy.py` | правило `tools.read_only_analysis` |
| Git | `git/local.py` | `clone` (https/ssh, без подмодулей и hooks), `diff`, `diff_stat`, `apply_patch`, `rollback_to` |
| Определение стеков | `detection/stacks.py` | запасной вариант по расширениям файлов, если нет манифеста |
| Redaction | `security/redaction.py` | параметры подписанных URL (`sig`, `X-Amz-Signature`, `token`, …) |
| CLI | `audit`, `baseline create`, `project add`, `tools list/pull` | коды выхода документированы |
| CI | job `sandbox` | gitleaks с проверкой checksum, `tools pull`, Docker/интеграционные тесты, **запрет пропусков**, self-audit репозитория |

Новые ADR: [0018](../adr/0018-executors-implementation.md), [0019](../adr/0019-tool-adapters-and-findings.md), [0020](../adr/0020-quality-gates-semantics-and-baseline.md). Руководство пользователя: [audit.md](../audit.md).

## 3. Фактически запущенные проверки (Linux, Docker 29.9.2, 2026-11-05)

| Проверка | Результат |
| --- | --- |
| `pytest --cov` (с gitleaks в PATH и Docker) | **556 passed, 0 skipped**, покрытие 96.86% |
| ruff, ruff format, mypy strict, import-linter (11 слоёв) | без замечаний |
| Набор правил Semgrep на фикстурах | 13 из 13 правил срабатывают, ошибок нет |
| Self-audit репозитория (`--backend docker -t gitleaks -t semgrep`) | PASS, exit 0, дерево не изменено |
| gitleaks `dir` и `git` по репозиторию | no leaks found |
| actionlint, zizmor | без замечаний |

Результаты Python 3.12 и CI (Windows/macOS + job `sandbox`) — в PR.

## 4. Найденные и исправленные проблемы

1. **`RLIMIT_AS` ломал Go-инструменты.** Gitleaks на хосте падал при старте: Go резервирует большое виртуальное адресное пространство. Ограничение убрано; лимиты памяти обеспечивает только Docker (ADR-0018).
2. **Неявный выход в сеть.** `docker run` сам скачивал отсутствующий образ, что нарушает `local-only` (AC-05). Введены `--pull never` и явная команда `tools pull`.
3. **Утечка подписанного URL.** Ошибка скачивания образа содержала URL CDN с `sig=`/`hmac=` (временные учётные данные), redactor их пропускал. Добавлен шаблон, ошибки инструментов сокращаются.
4. **`semgrep --version` зависал без сети** (проверка обновлений). Добавлены `--disable-version-check`, переменные окружения и короткий таймаут для проверки версии.
5. **Искажённый вывод инструмента ронял задачу** (`AttributeError`) вместо «не удалось разобрать». Разбор строже, runner ловит ошибку, гейт получает fail-closed.
6. **Истёкший baseline продолжал подавлять находки.** Исправлено: просроченные исключения снова учитываются.
7. **DAG-ошибки при сборке** (найдены первым же запуском): неверные capability у discovery-шагов; отчёт не зависел от сканов, поэтому таблица инструментов была пустой.
8. **Проекты на Python без манифеста не распознавались**, и ruff не запускался. Добавлено определение по расширениям файлов.
9. **Dogfooding:** gitleaks нашёл в моём новом тесте литерал `access_token=…`. Значение теперь собирается динамически, иначе упал бы CI-job `repo-security`.

## 5. Ограничения

- **Образ ruff (ghcr.io)** нельзя скачать в моём окружении: прокси блокирует CDN GitHub. Digest закреплён через registry API; Docker-тесты с ruff выполняются в CI (job `sandbox`), локально ruff проверен на хосте.
- **Сеть песочницы** в S3 либо полностью выключена, либо (`execution.docker.network: bridge`) открыта целиком. Egress-прокси с allow-list — B-205 (S8).
- **Windows:** у Local Executor есть только таймаут и ограничение вывода; Job Objects — B-206 (S9).
- **Гейты Build, Tests и Coverage** пока SKIPPED с указанием причины: адаптеры стеков (PHPStan, ESLint, pytest, go test, cargo, …) — S7. `--strict` превращает такие SKIPPED в FAIL.
- **Semgrep** по умолчанию не сканирует каталоги `tests/` (его встроенный `.semgrepignore`); отражение исключений в отчёте — B-225.
- **Клонирование по URL** идёт на хост в отдельный каталог (без hooks и подмодулей), а не внутрь песочницы.

## 6. План Sprint S4 — GitHub adapter, reusable workflows, PR/report integration

Приёмка: «CI проходит на пилотном репозитории».

1. **Порт `RepositoryHost`** и адаптер GitHub (REST/GraphQL на httpx): GitHub App и fine-grained token, ограничения по rate limit.
2. **Push ветки и создание PR**: запрет push в защищённые ветки и auto-merge (FR-17); approval `pr_create`.
3. **Check runs** с результатами гейтов; PR-комментарий с отчётом без дублей (маркер + upsert, FR-10).
4. **Отчёты SARIF 2.1.0 и JUnit**; `ai-coding report export`.
5. **Reusable workflows** (`workflow_call`): `quality`, `security`, `test-python`, `test-node`, `test-php`, `test-go`, `test-rust`, `ai-review`, `release`. Минимальные permissions, SHA-pinning, read-only workflow для PR из форков.
6. **GitHubActionsExecutor** (dispatch, ожидание, артефакты).
7. **Пилотный репозиторий**: подключение reusable workflows, CI зелёный; проверка AC-09 на двух стеках.
