# Аудит проекта (`ai-coding audit`)

Read-only аудит (FR-06; приёмка S3 и AC-02 в S7). LLM не нужен; `--triage` добавляет рекомендательную оценку Security Agent. Решения: [ADR-0018](adr/0018-executors-implementation.md), [ADR-0019](adr/0019-tool-adapters-and-findings.md), [ADR-0020](adr/0020-quality-gates-semantics-and-baseline.md), [ADR-0027](adr/0027-project-tools-and-offline-data.md), [ADR-0028](adr/0028-s7-agents-and-advisory-triage.md).

## Быстрый старт

```bash
ai-coding init /path/to/project
```

```bash
ai-coding tools pull
```

```bash
ai-coding audit -C /path/to/project --backend docker --output reports/
```

`tools pull` — единственный шаг, которому нужна сеть. Он скачивает образы инструментов, закреплённые по digest, и офлайн-данные в кэш платформы (`$AI_CODING_CACHE_DIR` или `~/.cache/ai-coding`): БД Trivy, БД OSV, toolchain Rust с Clippy. `tools pull --refresh` обновляет снимок БД. Сам аудит работает в песочнице без сети (`--network none`) и никогда не скачивает образы неявно (`--pull never`).

## Что происходит

| Фаза | Шаги |
| --- | --- |
| discovery | конфигурация, состояние git, стеки, **снимок рабочего дерева**, подавления инструментов (B-225) |
| execution | сканеры по стекам (таблица ниже); `--triage` — рекомендательная оценка находок Security Agent |
| verification | Quality Gates → **проверка целостности** (если дерево изменилось, run → FAILED) → отчёт JSON + Markdown |

## Инструменты

| Инструмент | Категория | Стеки | Как работает |
| --- | --- | --- | --- |
| Gitleaks | secrets | все | анализатор |
| Semgrep | SAST | все | анализатор, офлайн-правила |
| Trivy | зависимости, конфигурация | все | офлайн-БД из кэша (`--skip-db-update --offline-scan`) |
| OSV-Scanner | зависимости | все | офлайн-БД из кэша (`--offline-vulnerabilities`) |
| Ruff | lint | Python | анализатор |
| mypy | типы | Python (если настроен) | инструмент проекта |
| PHPStan, Pint | типы, стиль | Laravel/PHP | инструмент проекта (`vendor/bin`) |
| ESLint, `tsc` | lint, типы | Node/React/TypeScript | инструмент проекта (`node_modules`) |
| `go test` (+покрытие), golangci-lint | тесты, lint | Go | инструмент проекта, копия дерева |
| `cargo test`, Clippy | тесты, lint | Rust | инструмент проекта, копия дерева; Clippy из toolchain в кэше |
| Playwright | E2E-тесты | Node/React/TypeScript (есть `playwright.config.*`) | инструмент проекта в образе Playwright 1.56.1 с браузерами, копия дерева, без сети. Проект на другой версии `@playwright/test` — SKIPPED с причиной. Упавший тест — находка с файлом и строкой (S9) |
| k6 | нагрузочные тесты (гейт `tests`) | все (есть `*.k6.js`, `k6/*.js` или `performance_scripts`) | скрипты проекта против тестируемой системы. Доступ только к `quality_gates.tests.performance_hosts` через egress-прокси. Пересечённый порог (`thresholds`) — находка со значениями метрик (S9) |

Инструменты проекта выполняют код проекта и работают **только в Docker-песочнице**. Проект монтируется только для чтения. Если нужна установка зависимостей или инструмент пишет в дерево, используется копия, поэтому репозиторий не меняется. Установка зависимостей — шаг с сетью: нужны `execution.docker.network: bridge` и разрешение egress к реестру. С S9 контейнер установки попадает в изолированную сеть, где единственный выход — allowlist-прокси: доступны только хосты реестра, без DNS и прямых маршрутов, а адреса из частных сетей запрещены (B-205, ADR-0033). Второй шаг с сетью — k6 к хостам тестируемой системы. Иначе инструмент **SKIPPED** с точной причиной (например, `dependencies must be installed first, but execution.docker.network is 'none'`). Неприменимый инструмент (нет манифеста или конфигурации) тоже SKIPPED. Инструмент, который запустился, но не дал результата, — FAIL.

## Бэкенды

| `--backend` | Изоляция | Когда использовать |
| --- | --- | --- |
| `docker` (рекомендуется) | контейнер без сети, корень только для чтения, без capabilities, non-root, лимиты CPU/RAM/pids, проект смонтирован только для чтения | всегда, если доступен Docker |
| `local` | бинарники с хоста из allow-list, окружение из allow-list, таймауты, rlimits (POSIX), убийство дерева процессов | без Docker; инструменты должны быть в `PATH` |

На хосте политика разрешает только зарегистрированные инструменты анализа, которые читают проект и не выполняют его код (правило `tools.read_only_analysis`).

## Результат и коды выхода

| Код | Значение |
| --- | --- |
| 0 | PASS: ни один блокирующий гейт не провален |
| 1 | FAIL: провален блокирующий гейт, или run завершился ошибкой (например, нарушена целостность) |
| 2 | Docker недоступен / проект не найден |
| 3 | Неверные опции (`github-actions` до S4, конфигурация Semgrep из реестра, неизвестный инструмент) |

В отчёте (схема v2) также есть:

- **воспроизводимость**: версия инструмента, digest образа, снимок офлайн-БД;
- **подавления на уровне инструментов**: ignore-файлы, inline-маркеры, неявный список игнорирования Semgrep. Они обходят baseline, поэтому показываются ревьюеру;
- **triage** (с `--triage`): вердикты Security Agent у находок. Это рекомендация, гейты она не меняет. Файлы с секретами в модель не отправляются. Если провайдер внешний, прогон ждёт approval (`ai-coding audit --resume RUN`).

Гейты: PASS / FAIL / SKIPPED, у каждого есть причина и доказательства. Если инструмент не дал результата, гейт получает **FAIL** (fail closed). `--strict` превращает SKIPPED блокирующего гейта в FAIL.

## Baseline существующих проблем

```bash
ai-coding baseline create reports/audit-report.json --owner team-core --reason "legacy, JIRA-123" --expires 2027-03-31
```

Файл `.ai-coding-baseline.json` коммитится в репозиторий и проходит ревью. Принятые находки не влияют на гейты до даты `expires_at`, после неё снова считаются.

## Опции

| Опция | Описание |
| --- | --- |
| `-C DIR` | Каталог проекта |
| `-b, --backend local\|docker` | Переопределить `execution.backend` |
| `-t, --tool NAME` | Запустить только указанные инструменты (повторяемая) |
| `--semgrep-config FILE` | Свои правила Semgrep (только локальный файл) |
| `--strict` | SKIPPED блокирующего гейта = FAIL |
| `-o, --output DIR` | Записать `audit-report.json` и `audit-report.md` |
| `--json` | Вывести JSON-отчёт в stdout |
| `--triage` | Рекомендательный triage Security Agent (нужен `ai`) |
| `--resume RUN` | Продолжить прогон, ожидающий approval |
| `--dry-run` | Показать план и обработчики, ничего не выполнять |

## Импорт проекта

```bash
ai-coding project add https://github.com/org/repo.git --dest ./repo
```

Принимаются только `https://`, `ssh://` и `user@host:path`. `file://`, локальные пути и `ext::` отклоняются. Подмодули не загружаются, hooks отключены, учётные данные не запрашиваются. Без `--full-history` клон неглубокий (`--depth 1`).
