# Отчёт Sprint S9 — Hardening, нагрузочные и security-тесты, документация, релиз v2.1

Дата: 2026-11-05 · Ветка: `claude/cool-sagan-ho737s` · Приёмка ТЗ §16: «Все AC-01…AC-10 + v2.1». ТЗ §20: релиз только после прохождения всех AC и при наличии эксплуатационной инструкции.

## 1. Результат приёмки

| Критерий | Чем подтверждается | Результат |
| --- | --- | --- |
| **AC-01…AC-10** | Матрица приёмки в CI (job sandbox, Docker): каждый AC — автоматический тест с маркером `acceptance`. Плагин роняет прогон, если AC не покрыт, упал или **пропущен** ([acceptance.md](../acceptance.md)) | ✅ «AC-01…AC-10: all accepted» |
| B-804: автоматизация AC | Новые сценарии AC-05 (локальная модель, записывается каждое соединение), AC-06 (уязвимый PR), AC-09 (два стека по `pilots.yml`), AC-10 (dry-run, approval, отмена, экспорт журнала); остальные AC размечены в существующих тестах | ✅ |
| B-800: нагрузка (k6) | `loadtest/`: API в 2 процессах + 2 worker'а на PostgreSQL. Пороги: ≥ 10 параллельных прогонов, все завершены, p95 чтения < 500 мс, ошибок < 1 % | ✅ CI: 60/60 прогонов, до 20 одновременно, p95 66,5 мс, 0 ошибок |
| B-603: Playwright и k6 для проектов | Адаптеры с образами по digest; тесты на фикстуре Playwright и на сервисе-цели k6 в Docker | ✅ |
| B-801: security-тесты | Права по **всем** маршрутам OpenAPI; Schemathesis (позитивный и негативный fuzzing); Hypothesis для конфигурации и redaction; 26 проб выхода из песочницы | ✅ найдено и исправлено 6 дефектов (§6) |
| B-802: SBOM, Sigstore, SLSA | `scripts/sbom.py` (CycloneDX: пакет по `uv.lock`, Web UI, образ через Syft); workflow `release-platform.yml` с cosign keyless, SLSA provenance и SBOM attestations, черновик релиза | ✅ тесты SBOM в CI; workflow проверен actionlint и zizmor (выполнится по тегу) |
| B-803: эксплуатация | [operations.md](../operations.md) (ввод в эксплуатацию, операции, обновление, мониторинг и алерты, backup, безопасность, масштабирование, проверка подлинности релиза), [runbook.md](../runbook.md) (10 сценариев инцидентов) | ✅ |
| B-805: gVisor (P2) | `execution.docker.runtime: runsc`; CI ставит runsc (закреплённый релиз, SHA-512) и проверяет песочницу на gVisor | ✅ CI: `test_gvisor.py` 2 passed |
| B-806: релиз v2.1 | Версия 2.1.0, [CHANGELOG](../../CHANGELOG.md), workflow релиза | ✅ готово; **тег ставит человек** |
| B-807 | Настоящий S3-сервер (VersityGW, проверяет SigV4), общий rate limiter в БД, rootless/удалённый Docker | ✅ |
| B-205: egress allowlist | Шаги с сетью — во внутренней сети Docker с единственным выходом через allowlist-прокси | ✅ CI: запрещённый хост, SSRF, обход прокси — отказ; разрешённый реестр — доступен |
| B-206: Job Objects (Windows) | Память и CPU всего дерева процессов, их число, завершение дерева | ✅ CI Windows (после исправления теста, §6 п. 12) |

## 2. Структура (новое в S9)

```
src/ai_coding/
  executors/egress.py         # шлюз: --internal сеть, прокси, решения, Gateway.denied()
  executors/egress_proxy.py   # allowlist-прокси на stdlib: CONNECT :443 / HTTP :80, без частных адресов
  executors/winjob.py         # Job Objects (ctypes)
  security/endpoints.py       # локальный ли endpoint модели (local-only)
  tools/e2e.py                # PlaywrightAdapter, K6Adapter
  persistence/migrations/versions/0003_rate_limits.py
  api/limits.py               # + SharedRateLimiter
loadtest/  server.js  run_load.py
scripts/sbom.py
.github/workflows/release-platform.yml
docs/  acceptance.md  operations.md  runbook.md  adr/0032…0035
CHANGELOG.md
tests/
  acceptance_plugin.py        # матрица AC, отчёт JSON/Markdown
  acceptance/                 # AC-05, AC-06, AC-09, AC-10
  security/                   # test_api_access, test_api_fuzz, test_config_fuzz, test_redaction_fuzz, test_sandbox_escape
  integration/                # test_egress, test_gvisor, test_s3_real, test_e2e_tools_docker, test_supply_chain
  unit/                       # test_egress_proxy, test_e2e_tools, test_endpoints, test_loadtest, test_shared_limiter,
                              # test_winjob, test_acceptance_plugin
  fixtures/pilot-playwright, fixtures/tools/{k6-summary.json, playwright-report.json}
```

## 3. Архитектурные решения

- [ADR-0032](../adr/0032-acceptance-load-and-security-testing.md) — приёмочная матрица как условие релиза (пропуск ≠ приёмка), нагрузочный тест k6 на PostgreSQL, security-тесты.
- [ADR-0033](../adr/0033-sandbox-egress-and-e2e-tools.md) — egress только через allowlist-прокси во внутренней сети без DNS; Playwright и k6 как инструменты проектов.
- [ADR-0034](../adr/0034-server-scaling-and-isolation.md) — несколько процессов API, общий rate limiter в БД с пакетной синхронизацией, размер пула БД, rootless или удалённый Docker, S3.
- [ADR-0035](../adr/0035-runtime-isolation-and-release.md) — gVisor по выбору оператора, Job Objects на Windows, процесс релиза (SBOM, Sigstore, SLSA, черновик и одобрение человеком).

## 4. Реализованные компоненты

| Компонент | Ключевое |
| --- | --- |
| Приёмка | Маркер `acceptance("AC-NN")`, `--acceptance-report`, таблица в summary job'а CI |
| Нагрузка | Сценарии reads / runs / sampler; метрика одновременных прогонов; сводка в Markdown |
| Производительность API | Кэш проверенной конфигурации (путь, mtime, размер), `serve --workers N`, настраиваемый пул БД (10 + 10) |
| Единый формат ошибок | Ошибки фреймворка (404/405 и т. п.) в том же конверте, что и ошибки API |
| local-only | Endpoint модели должен быть loopback/локальным и разрешаться в локальный адрес |
| YAML | Лимит раскрытия алиасов (100 000 узлов) против «billion laughs» |
| Redaction | Токены `aic_…` маскируются |
| Rate limiter | Скользящая минута, локальный счёт + синхронизация раз в 0,5 с вне блокировки, откат на локальный счёт при сбое БД |
| Docker | Переменные удалённого/rootless демона (`DOCKER_HOST`, TLS, SSH) для каждого вызова; `--runtime` |
| Egress | Внутренняя сеть, `--dns 0.0.0.0`, прокси от nobody read-only; `host:port`; отказы в stderr шага |
| Playwright / k6 | Версия `@playwright/test` должна совпадать с образом; k6 только к `performance_hosts`, пороги → находки |
| Runner | `node_modules`/`vendor` монтируются read-only в копию дерева |
| Job Objects | Память дерева, CPU, число процессов, `KILL_ON_JOB_CLOSE`, завершение по таймауту |
| SBOM / релиз | CycloneDX × 3, `SHA256SUMS`, cosign, attestations, PyPI только по переменной |

## 5. Фактически запущенные проверки (Linux, Docker, 2026-11-05)

| Проверка | Результат |
| --- | --- |
| `pytest`, Python 3.13 (Docker, PostgreSQL, S3) | **925 passed, 9 skipped** по окружению: нет runsc (установка здесь запрещена), Windows-тесты, нет проверяемого HTTPS из контейнеров (TLS перехватывается), не подготовлены trivy DB / clippy / образ ruff, прокси на loopback хоста. Все эти тесты выполнены в CI |
| `pytest`, Python 3.12 (отдельное окружение) | **925 passed, 9 skipped** (те же) |
| ruff, ruff format, mypy strict (169 файлов), import-linter (2 контракта) | без замечаний |
| Дрейф схемы конфигурации; `uv lock --check` | нет / OK |
| actionlint; zizmor `--offline` | без замечаний |
| Web UI: `tsc`, Vitest, `vite build` | OK, 5 passed, 156 КБ JS |
| Нагрузочный тест локально | пороги пройдены; p95 чтения 515–625 мс на шумной VM при любом backend лимитера (в CI — 62–66 мс) |

CI на GitHub, run [42110402088](https://github.com/aitmaganbetov/ai-coding-platform/actions/runs/42110402088) (коммит `2150f8e`):
- **sandbox**: **242 passed, 0 skipped** (пропуск запрещён). В том числе:
  - все AC-01…AC-10 — «all accepted»;
  - песочница на gVisor (2);
  - egress с интернетом (2);
  - Playwright и k6 (2);
  - SBOM пакета, Web UI и образа (4);
  - VersityGW (4);
  - PostgreSQL (5);
  - security (11).

  Самоаудит репозитория (gitleaks, Semgrep) — 0 находок, репозиторий не изменён.
- **Load test**: 60/60 прогонов, максимум 20 одновременно, p95 чтения 66,5 мс, p99 181 мс, 0 ошибок из 3249 запросов.
- Lint, Web UI, образ сервера, wheel — зелёные.
- Тесты Ubuntu и macOS — зелёные.
- **Windows**: 892 passed, 1 failed — `test_process_count_is_limited`. Ошибка оказалась в самом тесте (§6 п. 12), исправлено в `2161bf0`. Тесты лимита памяти и завершения дерева на Windows прошли.

Итоговый CI по исправлению — в PR.

## 6. Найденные и исправленные проблемы

1. **DoS через YAML «billion laughs»** в `.ai-coding.yml` и ответах `create` (fuzzing конфигурации): разбор раскрывал алиасы без предела. Добавлен лимит размера раскрытого документа.
2. **`security.network: local-only` пропускал внешний `base_url`** у Ollama/vLLM (AC-05): модель «локального» провайдера могла смотреть наружу. Конфигурация отклоняется, фабрика LLM дополнительно проверяет разрешение имени.
3. **Токены `aic_…` не маскировались** в логах (fuzzing redaction).
4. **Ошибки фреймворка шли не в едином формате API** (Schemathesis, негативные случаи): 404/405 Starlette возвращали `{"detail": …}`.
5. **p95 4,9 с под нагрузкой.** Разбор YAML и JSON Schema на каждый запрос к прогону. Исправлено кэшем проверенной конфигурации и несколькими процессами API.
6. **Пул 10 + 30 соединений превышал `max_connections` PostgreSQL**: 4 прогона падали под нагрузкой. Пул стал настраиваемым, по умолчанию 10 + 10; правило расчёта добавлено в документацию.
7. **Общий rate limiter с записью на каждый запрос** ставил все запросы клиента в очередь на блокировку одной строки: p95 вырос с 0,4 до 1,9 с. Сделана пакетная синхронизация раз в 0,5 с вне блокировки процесса; превышение ограничено величиной `rate × 0,5 с` на процесс.
8. **Инструмент, пишущий в копию дерева, терял `node_modules`/`vendor` проекта** (тест Playwright). Теперь они монтируются read-only.
9. **k6:** `--no-summary` отключал и `--summary-export`, а сводка в JSON занимает несколько строк. Исправлены цикл запуска и разбор.
10. **Docker 29 не заполняет `.NetworkSettings.IPAddress`**: адрес прокси берётся из `.Networks`.
11. **Обход SSRF-проверки прокси через `NO_PROXY` и имя с точкой в конце** (`localhost.`) — найдено тестом; имена нормализуются.
12. **Тест лимита процессов Job Object падал на Windows в CI.** Лимит работал, ошибка была в подсчёте процессов в тесте. В venv uv на Windows `sys.executable` — лаунчер, который запускает настоящий интерпретатор дочерним процессом, поэтому каждый Python занимал в задании 2 слота. Тесты запускают базовый интерпретатор. Операторам стоит учитывать: `max_processes` считает и лаунчеры.
13. **Обёртка `build_argv` в тесте AC-05** не принимала новый аргумент `egress`. Это поймала матрица приёмки в CI.

### Исправлено после ревью кода (после merge S9, до тега v2.1.0)

Ревью merge-коммита S9 нашло 10 дефектов, большинство — в коде самого S9. Все исправлены. На каждый написан регрессионный тест, и он падает на прежнем коде.

| # | Где | Дефект | Исправление | Тест |
| --- | --- | --- | --- | --- |
| 1 | `config/loader.py` | Защита от «billion laughs» зацикливалась на узле, содержащем себя (`a: &x [*x]`, воспроизведено), — DoS | Узлы на текущем пути обхода отслеживаются; такой документ отклоняется ошибкой «recursive YAML alias» | `test_config_fuzz.py` (5 документов в подпроцессе с таймаутом, и через валидатор) |
| 2 | `tools/project.py` | Симлинк `node_modules` → каталог хоста монтировался в песочницу | Зависимости копируются; каталог-симлинк пропускается; внутренние ссылки — только относительные и внутри проекта | `test_dependency_links_never_lead_out_of_the_project` |
| 3 | `tools/project.py` | `node_modules` только для чтения: EROFS для кэшей Vite/Babel/ESLint | Копия доступна для записи | `test_writable_copy_keeps_installed_dependencies_writable`, Playwright в Docker |
| 4 | `egress_proxy.py` | HTTP: чужой `Host` и второй запрос по keep-alive — domain fronting | Прокси пересобирает ровно один запрос: `Host` = цель, тело по `Content-Length`, `Connection: close` | `test_plain_http_cannot_front_or_smuggle_a_second_request` (на старом прокси чужой `Host` получал 200) |
| 5 | `executors/local.py` | `JOB_TIME` = таймаут, хотя считает CPU всех ядер | Бюджет = таймаут × ядра; везде используется действующий таймаут | `test_job_cpu_budget_covers_every_core` |
| 6 | `api/limits.py` | Недоступная БД задерживала каждый запрос | Пауза 1 → 30 с, одна проба за раз | `test_unreachable_database_is_not_retried_on_every_request`, `test_database_recovery_resets_the_back_off` |
| 7 | `api/limits.py` | Переполнение ключей стирало неотправленные счётчики | Сначала устаревшие и записанные ключи, затем отправка одной транзакцией | `test_key_overflow_never_loses_unsent_counts`, `…_drops_stale_and_synced_keys_first` |
| 8 | `llm_factory.py` | `local-only`: адрес проверялся один раз (DNS rebinding) | `LocalOnlyTransport`: разрешение и проверка на каждый запрос, подключение к проверенному IP; `Host` и SNI сохраняются | `test_every_request_is_checked_…`, `test_https_keeps_the_name_for_tls` |
| 9 | `executors/process.py` | Ошибка назначения Job Object оставляла процесс без надзора | Процесс убивается, шаг завершается ошибкой | `test_command_is_killed_when_its_job_cannot_be_applied` |
| 10 | `api/app.py` | `serve --workers N` на SQLite — свой лимит у каждого процесса | `auto` выбирает общий лимитер при нескольких процессах; `memory` — с предупреждением | `test_several_api_processes_share_the_limit_on_sqlite_too`, `test_app_from_env_uses_the_shared_limiter_on_sqlite` |

## 7. Ограничения и незавершённое

- **Публикация релиза не выполнена — это действия владельца:**
  - тег `v2.1.0` после merge в `main`;
  - окружения GitHub `release` (с reviewers) и `pypi`;
  - переменная `PYPI_PUBLISH`;
  - публикация черновика.

  Workflow релиза выполнится только по тегу; локально и в CI проверены его составные части (SBOM, сборка) и статический анализ.
- **Сторонний DAST-сканер не запускался.** HawkScan требует `HAWK_API_KEY`, его нет. DAST закрыт Schemathesis (fuzzing по OpenAPI) и тестами прав по всем маршрутам.
- **MinIO как таковой не запускался:** образы MinIO в среде недоступны. Используется VersityGW — другой S3-совместимый сервер, который сам проверяет SigV4.
- **Redis-адаптер очереди и OpenTelemetry** (опциональны в ТЗ) не реализованы. Общий rate limiter работает через БД.
- **Job Object назначается сразу после запуска процесса:** asyncio не создаёт приостановленный процесс. Потомок, порождённый в это мгновение, может остаться вне задания. Недоверенный код выполняется только в Docker-песочнице.
- **Остаточный риск egress:** внутри HTTPS-туннеля прокси не видит имя сервера TLS. Через разрешённое имя на общем CDN можно обратиться к другому сайту этого CDN (ADR-0033). Список хостов держите минимальным.
- **gVisor не проверен в этой среде** (установка runsc запрещена) — только в CI.
- **Локальная задержка под нагрузкой** (p95 ~0,5–0,6 с) — шум виртуальной машины разработки; в CI p95 66 мс.
- **e2e smoke Web UI** по-прежнему выполняется локально, не в CI.
- **OIDC/SSO** — backlog.

## 8. Дальнейший план

Все спринты S1–S9 по ТЗ завершены. После приёмки S9:
1. Владелец ставит тег `v2.1.0`. Перед этим нужно настроить окружения `release` и, при необходимости, `pypi`. Затем владелец одобряет job `release` и публикует черновик релиза.
2. Проверка опубликованного релиза по [operations.md § 8](../operations.md#8-проверка-подлинности-релиза).
3. Кандидаты на v2.2 из backlog:
   - OIDC/SSO;
   - Redis-очередь;
   - OpenTelemetry;
   - e2e smoke UI в CI;
   - DAST-сканер с ключом организации.
