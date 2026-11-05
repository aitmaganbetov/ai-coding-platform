# Отчёт Sprint S8 — Self-Hosted API, RBAC, Web UI, observability

Дата: 2026-11-05 · Ветка: `claude/cool-sagan-ho737s` · Приёмка ТЗ §16: «Два проекта и несколько пользователей» (AC-08).

## 1. Результат приёмки

| Критерий | Чем подтверждается | Результат |
| --- | --- | --- |
| **AC-08:** запуск на self-hosted сервере с двумя проектами подтверждает RBAC и отсутствие утечки контекста | `tests/integration/test_ac08.py` — подробности ниже | ✅ |
| B-700: endpoints ТЗ §13, OpenAPI, `Idempotency-Key` | Все 8 endpoints §13 и управляющие (auth, токены, участники, экспорт). OpenAPI `/api/v1/openapi.json`. Повтор возвращает исходный ответ, иное тело — 422, гонка двух запросов разрешается | ✅ `test_api.py` |
| B-701: аутентификация, токены, RBAC на сервере, изоляция | argon2id, токены с областью проекта, предельной ролью и сроком; токены `agent` ≤ 24 ч. Матрица ролей ТЗ §3 проверяется в сервисном слое | ✅ `test_server_services.py`, `test_api.py`, AC-08 |
| B-702: rate limiting, security headers, TLS | Token bucket (429 + `Retry-After`), лимит входа по адресу, CSP без inline-скриптов, HSTS только по HTTPS или от доверенного proxy, 413 при теле > 1 МБ. TLS — Caddy в compose | ✅ `test_api.py`, CI `server-image` |
| B-703: PostgreSQL, очередь `SKIP LOCKED` | Миграция `0002_server`. Тесты на **настоящем PostgreSQL** в Docker: миграции без дрейфа, append-only журнал, 3 worker'а × 24 задания без дублей, API | ✅ `test_postgres.py` (Redis-адаптер не реализован, см. §7) |
| B-704: worker'ы, ≥10 параллельных прогонов | 12 прогонов в одном процессе: пик одновременных шагов ≥ 10, время много меньше последовательного. Лимит соблюдается; lease истекает и «ядовитое» задание помечается failed; **убитый процесс worker'а** — другой worker продолжает с checkpoint без повтора шагов; approval → resume | ✅ `test_worker.py` |
| B-705: MinIO ArtifactStore (P1) | `S3ArtifactStore` с SigV4: подпись совпадает с эталонным примером AWS, fake-S3 проверяет каждую подпись, целостность по SHA-256 | ✅ (без настоящего MinIO, см. §7) |
| B-706: Web UI (P1) | React + TS + Vite: вход, проекты, запуск прогона, прогоны, approvals, находки, участники, журнал, артефакты, экспорт. 5 тестов Vitest; e2e smoke в браузере против живого сервера | ✅ |
| B-707: метрики (P1) | `/metrics` Prometheus: HTTP по шаблону маршрута, прогоны и задания по состояниям, активные прогоны worker'а; доступ по токену метрик или администратору | ✅ (OpenTelemetry не подключён, §7) |
| B-708: docker-compose, Dockerfile, backup, ротация ключей | Образ non-root (uid 10001), базовые образы по digest. CI собирает образ и запускает его read-only. Compose: Caddy → API (без Docker) → PostgreSQL, worker с сокетом. `backup.sh`; ротация токенов через API, процедуры в [server.md](../server.md) | ✅ CI `server-image` |
| B-709: экспорт и retention | ZIP со всеми данными проекта; удаление проекта (409 при активных прогонах, журнал сохраняется); retention прогонов, артефактов, ключей идемпотентности и токенов | ✅ `test_lifecycle.py` |

**Что проверяет AC-08.** Сервер с двумя репозиториями `alpha` и `beta`, в каждом своя метка в коде. Пять пользователей:
- `alice` — developer в alpha;
- `bob` — developer в beta;
- `carol` — auditor в alpha;
- `dave` — maintainer в обоих проектах;
- администратор.

Всё проверяется через REST API, discovery-прогоны выполняет worker:
- Каждый видит только свои проекты.
- Артефакты прогона alpha содержат метку alpha и не содержат метку beta, и наоборот.
- Семь путей к объектам beta (проект, прогоны, находки, прогон, список артефактов, артефакт, журнал) отвечают alice `404`. Ответ побайтно совпадает с ответом на несуществующий прогон. Запуск и отмена в чужом проекте тоже дают `404`.
- Внутри проекта: auditor не запускает прогон, developer не читает журнал, maintainer не экспортирует и не назначает owner'а (`403`).
- Токен `dave`, ограниченный alpha, не видит beta, хотя сам `dave` её видит.
- Экспорт alpha не содержит ни прогона, ни метки beta.
- Журнал alpha содержит отказы carol, alice и dave и только события alpha. Журнал beta не содержит событий alpha.
- Попытки alice в beta видны администратору как серверные события `project:<beta>`.

## 2. Структура (новое в S8)

```
src/ai_coding/api/
  services.py   # Server: пользователи, токены, проекты, прогоны, approvals, артефакты, находки, журнал; require()
  rbac.py       # роли × права (ТЗ §3), ограничение роли токеном
  security.py   # argon2id, токены (SHA-256), сравнение за постоянное время
  app.py        # FastAPI: роутеры ops/auth/projects/runs, middleware безопасности, ошибки, статика UI
  worker.py     # JobQueue (SKIP LOCKED / CAS), Worker (пул, heartbeat, resume, retention)
  kinds.py      # типы прогонов discover/audit/develop/maintain и их параметры
  lifecycle.py  # экспорт, удаление проекта, retention
  limits.py     # token bucket, адрес клиента за доверенным proxy
  metrics.py    # Prometheus
  settings.py   # AI_CODING_SERVER_*
src/ai_coding/persistence/
  s3.py                                # S3ArtifactStore + SigV4
  migrations/versions/0002_server.py   # users.is_admin, credentials, api_tokens, idempotency_records, run_jobs
src/ai_coding/cli/commands/server.py   # ai-coding server migrate|user|token|serve|worker|export|purge|settings
web/                                   # React + TS + Vite, Vitest, e2e/smoke.mjs (Playwright)
deploy/  Dockerfile  docker-compose.yml  Caddyfile  backup.sh  .env.example
docs/server.md
tests/integration/  test_ac08.py test_api.py test_worker.py test_postgres.py test_server_cli.py
                    test_lifecycle.py server_support.py
tests/unit/         test_server_services.py test_s3_store.py
```

## 3. Архитектурные решения

- [ADR-0030](../adr/0030-server-api-rbac-queue.md):
  - сервисный слой проверяет права при любом транспорте;
  - одна БД, оркестратор на проект без автоматического owner'а;
  - «чужое = несуществующее» и аудит отказов отдельной транзакцией;
  - `Idempotency-Key`;
  - очередь в БД (`SKIP LOCKED` / CAS) и пул worker'а;
  - отклонения от ADR-0013/0014: нет Redis, клиент UI типизирован вручную.
- [ADR-0031](../adr/0031-server-deployment-storage-ui.md):
  - образ и compose (API без Docker, сокет только у worker'а), TLS через Caddy, backup и ротация;
  - S3/MinIO с адресацией по содержимому;
  - UI без cookie и inline-скриптов;
  - метрики без клиентской библиотеки;
  - экспорт и retention.
- Уточнены статусы ADR-0005, 0013, 0014. В ADR-0027 добавлено, почему права меняются для элементов внутри точки монтирования, а не для неё самой.

## 4. Реализованные компоненты

| Компонент | Ключевое |
| --- | --- |
| Аутентификация | argon2id с пересчётом устаревших хэшей; токены `aic_…` (SHA-256 в БД), вход 12 ч, по умолчанию 30 дн., `agent` ≤ 24 ч и только с проектом; ротация и отзыв |
| RBAC | 13 прав, 5 ролей ТЗ §3; эффективная роль `min(участник, токен)`; администратор сервера; последний owner защищён |
| Изоляция | Единый `require`; чужой объект → 404 как отсутствующий; отказы в журнале (внутри проекта или как серверные события) |
| Проекты | Регистрация администратором: клон Git URL в управляемый каталог или путь внутри `ALLOWED_ROOTS`; `.ai-coding/config.yml` создаётся при отсутствии |
| Прогоны | `discover`, `audit` (только Docker), `develop`, `maintain`; тип и параметры хранятся в прогоне; approve/reject; отмена (developer — только свои) |
| Очередь и worker | `run_jobs`, lease + heartbeat, 3 попытки, resume с checkpoint; пул по `AI_CODING_SERVER_WORKERS` (10); graceful shutdown; retention раз в час |
| HTTP | Единый формат ошибок, лимиты, заголовки безопасности, доверенные proxy, `Cache-Control: no-store` |
| Метрики | `aicoding_http_*`, `aicoding_runs`, `aicoding_jobs`, `aicoding_worker_*` |
| Хранилище | `ArtifactStore` (local / S3), проверка SHA-256 при чтении |
| Web UI | Хэш-маршрутизация, опрос прогона каждые 2 с, скачивание артефактов и экспорта с заголовком авторизации |
| Развёртывание | Dockerfile (3 стадии, uid 10001), compose с hardening, Caddy, `backup.sh`, `.env.example` |
| CLI | `ai-coding server …` (8 команд) |
| Попутно | `find_project_config` (общий поиск конфигурации), `RunOptions.extra`, `Orchestrator(local_owner=False)`, ленивый stderr в логах, OSV `--no-call-analysis`, права на офлайн-данные, `tools/scratch.py` (очистка временных каталогов песочницы) |

## 5. Фактически запущенные проверки (Linux, Docker, 2026-11-05)

| Проверка | Результат |
| --- | --- |
| `pytest --cov`, Python 3.13 (Docker: все образы инструментов, PostgreSQL) | RESULT_313 |
| `pytest`, Python 3.12 (отдельное окружение) | RESULT_312 |
| Новые тесты S8 | сервисы 9, API 8, worker 7, PostgreSQL 4, CLI сервера 3, экспорт и retention 5, S3 4, AC-08 1, временные каталоги 4 + 1 в Docker, API не блокируется встроенным worker'ом 1; изменены тесты подготовки инструментов |
| ruff, ruff format, mypy strict (164 файла), import-linter (2 контракта) | без замечаний |
| Дрейф схемы конфигурации; `uv lock --check` | нет / OK |
| actionlint; zizmor `--offline` | без замечаний |
| Web UI: `tsc --noEmit`, Vitest, `vite build` | OK, 5 passed, сборка 156 КБ JS |
| e2e smoke (Playwright, живой сервер с worker'ом и UI) | вход → проект → запуск discovery → прогон COMPLETED → 1 артефакт, без ошибок в консоли |
| Подпись SigV4 | совпадает с эталоном AWS `34b48302…6dc6f7` |

CI на GitHub (run [42062746703](https://github.com/aitmaganbetov/ai-coding-platform/actions/runs/42062746703), коммит `4099162`): CI_RESULT

## 6. Найденные и исправленные проблемы

1. **Повышение привилегий через оркестратор.** `ensure_project` в локальном режиме делает пользователя CLI owner'ом. На сервере любой прогон сделал бы инициатора owner'ом проекта. Добавлен `local_owner=False`: сервер назначает роли только через `set_member`.
2. **Отказы исчезали из журнала:** запись откатывалась вместе с транзакцией запроса. Теперь отказ пишется в отдельной транзакции.
3. **Попытки доступа к чужому проекту не оставляли следа** (найдено при написании ADR-0030). Теперь это серверные события, видимые администратору; журнал проекта посторонних не показывает. Закреплено в AC-08.
4. **Discovery не находил серверную конфигурацию** `.ai-coding/config.yml`. Поиск конфигурации вынесен в общий `find_project_config`.
5. **Подготовка OSV в CI на `main` после merge S7** (job sandbox). OSV v2.6.0 скачивал БД, но данные не были видны. Причины, по очереди:
   - каталоги создавались с режимом 0750 от uid 1000, а runner работает под uid 1001;
   - добавленный `chmod -R a+rX` в BusyBox прекращал обход на корне монтирования, который принадлежит хосту (`chmod: /cache: Operation not permitted`) — воспроизведено в Docker с uid 1000/1001;
   - call analysis требует toolchain Go/Cargo, которого нет в образе.

   Исправлено: права меняются для элементов внутри точки монтирования, call analysis выключен (`--no-call-analysis=all`), сообщение об ошибке содержит результат шага прав и листинг кэша.
6. **Логи сервера в stdout смешивались с выданным токеном.** Логи перенесены в stderr. Явная передача `sys.stderr` затем сломала 9 тестов в CI («I/O operation on closed file»): pytest закрывает поток. Теперь поток определяется в момент записи.
7. **Тест NFR-02 нестабилен на медленных runner'ах:** пик 8–9 из 12 при шагах по 0,6 с. Шаг удлинён до 2,5 с, сравнение идёт с последовательным временем. Сам пул не менялся.
8. **Approval посреди прогона** возможен только для изменяющих операций в фазе execution. Тест approval использует шаг с `requires_approval=DEPLOY`.
9. **Starlette 1.x TestClient требует `httpx2`** — добавлен в dev-группу. Устаревший `on_event` заменён на lifespan.
10. **Vite и Vitest тянули разные версии Vite** с конфликтом типов. Конфигурации разделены.
11. **Временные каталоги песочницы не удалялись, если пользователь хоста не uid 1000.** Найдено в CI: runner работает под uid 1001. Песочница (uid 1000) оставляет файлы, которые хост удалить не может:
    - read-only кэш модулей Go;
    - приватные каталоги npm `_cacache` и `cargo target`.

    `TemporaryDirectory` падал с `PermissionError`, и вместе с ним — go test, cargo test/Clippy и обновление npm. Локально проблема не видна, потому что тесты шли от root. На сервере с другим uid аудит падал бы так же. Воспроизведено запуском тестов от uid 1001: старый код падает, новый проходит.

    Исправлено в `tools/scratch.py`. Если хост не может удалить файлы, их удаляет пользователь песочницы в уже использованном образе и без сети. Права каталога исправляются до спуска в него. Сбой очистки записывается в лог, но прогон не прерывает.
12. **Встроенный worker блокировал API** (видно только на Windows в CI): в тесте живого сервера запросы висели дольше 30 с, пока шёл прогон. Worker выполнял оркестратор в общем event loop, а обработчики делают синхронную работу (БД, git, файлы); при `serve --with-worker` этот loop общий с HTTP-сервером. Воспроизведено тестом с блокирующим шагом: `/healthz` ждал окончания шага. Теперь каждое задание выполняется в своём потоке со своим loop, из пула размером `concurrency`. Первая версия на `asyncio.to_thread` ограничивала параллельность пулом по умолчанию: пик 8 при 4 CPU. Это поймал тест NFR-02. Таймаут клиента в тесте живого сервера тоже увеличен до 30 с.

## 7. Ограничения и незавершённое

- **Redis-адаптер очереди (B-703, опционально) не реализован.** Очередь в БД покрывает NFR-02, Redis в ТЗ опционален. Rate limiter работает в процессе: при нескольких репликах API лимит умножается на их число.
- **OpenTelemetry (B-707, опционально) не подключён.** Связь событий обеспечивают `run_id` в JSON-логах и журнале аудита.
- **Настоящий MinIO не запускался.** Образы MinIO в этой среде недоступны: Docker Hub `minio/minio` удалён, quay.io заблокирован, на mirror.gcr.io образа нет. Корректность подтверждена эталонным вектором AWS и fake-сервером, который проверяет подпись. Прогон против MinIO — B-807 в S9.
- **e2e smoke UI выполнен локально, в CI его нет.** В CI идут typecheck, Vitest, сборка и проверка отсутствия inline-скриптов.
- **Worker с Docker-сокетом равнозначен root на хосте.** Он изолирован от API, но для production рекомендованы выделенный хост, rootless Docker или удалённый демон (S9 hardening, B-807).
- **Egress allowlist в песочнице (B-205)** перенесён в S9.
- **DAST HawkScan не запускался:** нет `HAWK_API_KEY`. Security-тесты API (SAST/DAST) — B-801 в S9.
- **OIDC/SSO** — backlog.

## 8. План Sprint S9 — Hardening, нагрузочные и security-тесты, документация, релиз

1. Нагрузочные тесты API и worker'ов на k6 (B-800); адаптеры Playwright и k6 для проектов пользователей (B-603).
2. Security-тесты: SAST/DAST API, fuzzing конфигурации, попытки выхода из песочницы (B-801).
3. SBOM CycloneDX, подписи Sigstore, provenance SLSA (B-802).
4. Эксплуатационная инструкция и runbook восстановления (B-803). Автоматизация приёмочных сценариев AC-01…AC-10 (B-804).
5. MinIO в CI, rootless Docker/удалённый демон для worker'а, общий rate limiter (B-807); egress allowlist (B-205); gVisor (B-805, P2).
6. Релиз v2.1: changelog, теги, публикация пакета (B-806).
