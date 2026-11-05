# Self-Hosted сервер: API, RBAC, worker'ы, Web UI

Многопользовательский режим платформы (ТЗ §3, §13, §15): REST API, роли в проектах, очередь прогонов в БД, worker'ы с Docker-песочницей и Web UI. Решения описаны в [ADR-0030](adr/0030-server-api-rbac-queue.md) и [ADR-0031](adr/0031-server-deployment-storage-ui.md).

```
Браузер / CLI / CI ──HTTPS──▶ Caddy (TLS) ──▶ api (FastAPI, Web UI) ──▶ PostgreSQL
                                                                         ▲
                                         worker ×N (≥10 прогонов каждый) ┘──▶ Docker-песочницы
```

API не запускает контейнеры. Worker забирает задания из очереди в БД и выполняет прогоны тем же оркестратором, что и CLI: FSM, checkpoints, approvals, аудит.

## Быстрый старт (docker-compose)

```bash
cp deploy/.env.example deploy/.env
```

Заполните в `deploy/.env` домен, длинный случайный `POSTGRES_PASSWORD` и каталог данных `AI_CODING_DATA`. Файл `.env` не коммитится.

```bash
sudo install -d -o 10001 -g 10001 -m 0750 /srv/ai-coding
```

```bash
export DOCKER_GID=$(getent group docker | cut -d: -f3)
```

```bash
docker compose -f deploy/docker-compose.yml up -d --build
```

```bash
docker compose -f deploy/docker-compose.yml run --rm api migrate
```

```bash
docker compose -f deploy/docker-compose.yml run --rm api user add admin --admin
```

Пароль запрашивается интерактивно (минимум 12 символов; `--password-stdin` для автоматизации). Затем откройте `https://<домен>/` и войдите.

Образ: многоступенчатая сборка, все базовые образы закреплены по digest. Процесс работает от непривилегированного пользователя uid 10001. Контейнеры запускаются с `read_only`, `cap_drop: ALL` и `no-new-privileges`. Доступ к Docker есть только у worker'а; песочницы его не получают (ADR-0018).

## Локальный запуск без Docker

```bash
uv sync --extra server
```

```bash
ai-coding server migrate
```

```bash
ai-coding server user add admin --admin
```

```bash
ai-coding server serve --with-worker --ui web/dist
```

`--with-worker` запускает worker в процессе API (удобно для одного хоста). Для нескольких worker'ов запускайте `ai-coding server worker` отдельно.

`serve --workers N` запускает N процессов API на одном порту. Один процесс Python упирается в GIL примерно на 100 запросах в секунду, поэтому разумно брать один процесс на ядро, оставив ядра worker'ам. С `--with-worker` этот режим несовместим. Rate limiter работает в каждом процессе отдельно, поэтому лимит умножается на N. Web UI собирается командой `npm ci && npm run build` в `web/`.

## Настройки

| Переменная | По умолчанию | Назначение |
| --- | --- | --- |
| `AI_CODING_SERVER_DATA_DIR` | `~/.local/share/ai-coding-server` | Клоны проектов, SQLite, артефакты |
| `AI_CODING_SERVER_DATABASE_URL` | `sqlite:///<data dir>/server.db` | В production — `postgresql+psycopg://…` (extra `postgres`) |
| `AI_CODING_SERVER_WORKERS` | `10` | Параллельных прогонов на процесс worker'а (1…256, NFR-02) |
| `AI_CODING_SERVER_ALLOWED_ROOTS` | пусто | Каталоги на сервере, которые можно регистрировать как проекты (через `:`). Пусто — только клон по Git URL |
| `AI_CODING_SERVER_RATE_LIMIT` | `120` | Запросов в минуту на пользователя или токен |
| `AI_CODING_SERVER_LOGIN_RATE_LIMIT` | `10` | Попыток входа в минуту с одного адреса |
| `AI_CODING_SERVER_TRUSTED_PROXIES` | пусто | Адреса или имена proxy, которым доверяется `X-Forwarded-For`/`-Proto` |
| `AI_CODING_SERVER_METRICS_TOKEN` | пусто | Отдельный секрет для `/metrics`; пусто — только администратор |
| `AI_CODING_SERVER_RETENTION_DAYS` | `0` | Срок хранения завершённых прогонов. `0` — срок из `artifacts.retention_days` проекта при `server purge` |

| `AI_CODING_SERVER_DB_POOL_SIZE` / `AI_CODING_SERVER_DB_MAX_OVERFLOW` | `10` / `10` | Соединения PostgreSQL на один процесс: постоянные / дополнительные под нагрузкой |

`ai-coding server settings` показывает действующие значения без секретов.

**Размер пула.** Сумма по всем процессам — API-процессы `serve --workers N` плюс worker'ы — не должна превышать `max_connections` PostgreSQL (по умолчанию 100): `процессы × (POOL_SIZE + MAX_OVERFLOW) < max_connections`. При превышении PostgreSQL отказывает в соединении (`too many clients`), и запросы завершаются ошибкой 500. Нагрузочный тест воспроизвёл это при пуле 10 + 30 на 4 процесса.

## Роли и изоляция проектов

Роль назначается в каждом проекте отдельно. Права проверяет сервер (ТЗ §3), а не UI.

| Право | owner | maintainer | developer | auditor | agent |
| --- | :-: | :-: | :-: | :-: | :-: |
| Просмотр проекта, прогонов, находок, артефактов | ✅ | ✅ | ✅ | ✅ | ✅ |
| Прогоны discover/audit | ✅ | ✅ | ✅ | — | — |
| Прогоны с изменениями (develop/maintain) | ✅ | ✅ | ✅ | — | — |
| Отмена своих прогонов | ✅ | ✅ | ✅ | — | — |
| Отмена любых прогонов, approve/reject | ✅ | ✅ | — | — | — |
| Журнал аудита | ✅ | ✅ | — | ✅ | — |
| Участники, экспорт, удаление проекта | ✅ | — | — | — | — |

- Администратор сервера (`--admin`) регистрирует проекты и пользователей и имеет права owner во всех проектах.
- **Чужой проект неотличим от несуществующего.** На любой объект другого проекта (проект, прогон, артефакт, находки, журнал) ответ — `404` с тем же телом, что и для отсутствующего объекта. `403` возвращается только участнику проекта, у которого не хватает роли.
- Каждая операция и каждый отказ записываются в журнал аудита: actor, project_id, run_id, время, результат. Отказы сохраняются отдельной транзакцией и не откатываются вместе с запросом.
- Последнего owner'а проекта нельзя удалить или понизить.

## Токены API

```bash
ai-coding server token create ci-bot --name nightly --project <project-id> --role auditor --ttl-hours 720
```

- Токен показывается один раз (`aic_…`); в БД хранится только SHA-256.
- Токен с `--project` действует только в этом проекте. `--role` ограничивает права сверху: роль токена — меньшая из роли пользователя и `--role`.
- Токены роли `agent` обязаны быть привязаны к проекту и живут не дольше 24 часов (ТЗ §3: «ограниченно, короткоживуще»).
- Токен, ограниченный проектом, не может выпускать новые токены.
- Токены входа (`POST /auth/login`) живут 12 часов, остальные — 30 дней по умолчанию.

## REST API

Базовый путь `/api/v1`. Документ OpenAPI — `/api/v1/openapi.json`, интерактивная документация — `/api/docs`. Аутентификация: `Authorization: Bearer <token>`.

| Метод и путь | Назначение |
| --- | --- |
| `POST /auth/login`, `GET /me` | Вход, текущий пользователь и его проекты |
| `GET/POST /tokens`, `POST /tokens/{id}/rotate`, `DELETE /tokens/{id}` | Токены API |
| `POST /users` | Пользователь (администратор) |
| `GET/POST /projects`, `GET/DELETE /projects/{id}` | Проекты (ТЗ §13: `POST /projects`) |
| `PUT/DELETE /projects/{id}/members/{username}` | Роли участников |
| `GET /projects/{id}/export` | ZIP со всеми данными проекта (NFR-08) |
| `GET /kinds`, `GET/POST /projects/{id}/runs` | Типы прогонов, список и запуск (ТЗ §13) |
| `GET /runs/{id}`, `POST /runs/{id}/approve`, `POST /runs/{id}/cancel` | Статус, решение по approval, отмена (ТЗ §13) |
| `GET /runs/{id}/artifacts`, `GET /runs/{id}/artifacts/{artifact_id}` | Артефакты (ТЗ §13) |
| `GET /projects/{id}/findings` | Находки (ТЗ §13) |
| `GET /audit-events?project_id=…` | Журнал аудита (ТЗ §13) |
| `GET /healthz`, `GET /readyz`, `GET /metrics` (без `/api/v1`) | Живость, готовность БД, метрики Prometheus |

Типы прогонов: `discover`, `audit` (всегда в Docker-песочнице), `develop`, `maintain`. Параметры — объект `params`, схема видна в `GET /kinds`.

**Идемпотентность.** `POST /projects` и `POST /projects/{id}/runs` принимают заголовок `Idempotency-Key`. Повтор с тем же телом возвращает исходный ответ с `Idempotent-Replayed: true`. Тот же ключ с другим телом или путём — `422`. Ключи хранятся сутки.

**Ошибки** имеют единый вид `{"error": {"code": "...", "message": "..."}}`. `429` приходит с заголовком `Retry-After`, `413` — для тела больше 1 МБ.

## Безопасность HTTP (NFR-04)

- TLS терминирует Caddy с автоматическими сертификатами. API доверяет `X-Forwarded-*` только от адресов из `AI_CODING_SERVER_TRUSTED_PROXIES`. HSTS выставляется только для HTTPS-запросов.
- Заголовки: `Content-Security-Policy` (`default-src 'self'`, без inline-скриптов), `X-Content-Type-Options`, `X-Frame-Options: DENY`, `Referrer-Policy: no-referrer`, `Cache-Control: no-store` для `/api/`.
- Rate limiting — на пользователя/токен и отдельно на вход с одного адреса. Тип задаёт `AI_CODING_SERVER_RATE_LIMIT_BACKEND`:
  - `auto` (по умолчанию) — на PostgreSQL лимит **общий** для всех процессов API и реплик: счётчики хранятся в БД (таблица `rate_limit_counters`, ключи — SHA-256). На SQLite он работает внутри процесса.
  - `memory` и `database` задают тип явно.

  Общий лимитер считает по скользящей минуте. Каждый процесс накапливает счёт локально и раз в 0,5 с добавляет его в БД атомарным upsert. Поэтому за одно окно синхронизации процесс может пропустить запросов не больше, чем «скорость × 0,5 с». Запись в БД на каждый запрос выстраивала запросы в очередь на блокировке строки: в нагрузочном тесте p95 вырос с 0,4 до 1,9 с. Если БД недоступна, решает локальный счёт процесса.
- Web UI хранит токен в `sessionStorage` и отправляет его заголовком. Cookie не используются, поэтому CSRF неприменим.

## Очередь и worker'ы

- Прогон создаётся в API и ставится в очередь (`run_jobs`). Worker берёт задание под lease: в PostgreSQL через `FOR UPDATE SKIP LOCKED`, в SQLite через compare-and-set. Lease продлевается heartbeat'ом.
- Если worker упал, lease истекает. Задание забирает другой worker и продолжает прогон с последнего checkpoint (`resume`). После трёх неудачных попыток задание помечается `failed`.
- Approval останавливает прогон в `WAITING_APPROVAL`. Решение через `POST /runs/{id}/approve` ставит в очередь `resume`, когда ожидающих approvals не остаётся.
- `AI_CODING_SERVER_WORKERS` задаёт число параллельных прогонов на процесс. Масштабирование — дополнительные контейнеры `worker`.
- `SIGTERM` и `SIGINT`: worker перестаёт брать задания и дожидается текущих.

## Изоляция worker'а: rootless Docker и удалённый демон

Доступ к Docker-демону эквивалентен root на его хосте. API доступа к демону не имеет, но worker должен его иметь, чтобы запускать песочницы. Для production рекомендуется один из вариантов:

- **Rootless Docker** на хосте worker'а: демон работает от непривилегированного пользователя, и захват демона не даёт root на хосте. Worker найдёт демон через `DOCKER_HOST=unix:///run/user/<uid>/docker.sock` или `docker context use rootless` (`DOCKER_CONTEXT`). Файлы, которые песочница создаёт во временных каталогах, получают в rootless-режиме uid из диапазона subuid. Хост не может их удалить, поэтому их удаляет сам пользователь песочницы (`tools/scratch.py`).
- **Удалённый демон** (отдельная машина или VM для песочниц): `DOCKER_HOST=tcp://builder:2376` с `DOCKER_TLS_VERIFY=1` и `DOCKER_CERT_PATH`, либо `DOCKER_HOST=ssh://user@builder` с `SSH_AUTH_SOCK`. Пути монтирования интерпретирует демон, поэтому каталог данных (`AI_CODING_SERVER_DATA_DIR`, клоны и временные каталоги) и `TMPDIR` должны быть доступны демону **по тем же путям** — например, через общий NFS-том.

Executor передаёт `docker` CLI только эти переменные (`DOCKER_HOST`, `DOCKER_CONTEXT`, `DOCKER_TLS_VERIFY`, `DOCKER_CERT_PATH`, `SSH_AUTH_SOCK`, `XDG_RUNTIME_DIR` и т. п.). Секреты процесса платформы — ключи провайдеров, S3, GitHub — в окружение `docker` не попадают.

## Артефакты в MinIO / S3

В `.ai-coding/config.yml` проекта укажите `artifacts.store: minio` и задайте переменные worker'у и API:

| Переменная | Пример |
| --- | --- |
| `AI_CODING_S3_ENDPOINT` | `https://minio.internal:9000` |
| `AI_CODING_S3_BUCKET` | `ai-coding` |
| `AI_CODING_S3_ACCESS_KEY_ID` / `AI_CODING_S3_SECRET_ACCESS_KEY` | ключи сервисной учётной записи |
| `AI_CODING_S3_REGION` | `us-east-1` |

Объекты хранятся по адресу содержимого `projects/<имя>/<sha256>`. При чтении содержимое сверяется с хэшем. Подпись — AWS SigV4. В CI хранилище проверяется на настоящем S3-совместимом сервере VersityGW, который сам проверяет подписи: операции, постраничный `ListObjectsV2`, неверные ключи (`SignatureDoesNotMatch`), подмена объекта и серверный прогон с артефактами в bucket'е.

## Метрики и логи (NFR-07)

`/metrics` в формате Prometheus. Доступ — с `Authorization: Bearer $AI_CODING_SERVER_METRICS_TOKEN` или от администратора.

| Метрика | Тип |
| --- | --- |
| `aicoding_http_requests_total{method,route,status}` | counter |
| `aicoding_http_request_duration_seconds{method,route}` | histogram |
| `aicoding_runs{state}`, `aicoding_jobs{state}` | gauge |
| `aicoding_worker_active_runs`, `aicoding_worker_jobs_total{outcome}` | gauge / counter (процесс worker'а) |

Логи пишутся в stderr в JSON с `run_id` и без секретов (redaction из S3). OpenTelemetry не подключён, см. [S8-report](sprints/S8-report.md).

## Резервное копирование и восстановление

```bash
deploy/backup.sh /var/backups/ai-coding 14
```

Скрипт делает `pg_dump` в gzip с правами `0600` и удаляет копии старше 14 дней. Запускайте его из cron. Восстановление:

```bash
gunzip -c /var/backups/ai-coding/aicoding-<время>.sql.gz | docker compose -f deploy/docker-compose.yml exec -T postgres psql -U aicoding -d aicoding
```

Каталог данных (`AI_CODING_DATA`: клоны и локальные артефакты) копируйте средствами файловой системы. Артефакты в MinIO — политиками bucket'а.

## Ротация ключей

| Секрет | Как сменить |
| --- | --- |
| Токен API | `POST /tokens/{id}/rotate`: выпускается новый, старый отзывается сразу. Либо `ai-coding server token create` и затем `token revoke` |
| Пароль пользователя | `ai-coding server user password <имя>`. Хэши argon2id пересчитываются при входе, если параметры изменились |
| `AI_CODING_SERVER_METRICS_TOKEN` | Новое значение в `.env`, затем `docker compose up -d api` и обновить scrape-конфигурацию |
| Пароль PostgreSQL | `ALTER USER aicoding PASSWORD '…'` в `psql`, затем новое значение `POSTGRES_PASSWORD` в `.env` и `docker compose up -d api worker` |
| Ключи S3, ключи LLM-провайдеров | Новое значение в окружении worker'а и API и перезапуск. Значения в коде и в БД не хранятся |

## Экспорт и удаление данных (NFR-08)

- `GET /projects/{id}/export` (owner) или `ai-coding server export <project> --user <owner> --output project.zip`. ZIP содержит проект, прогоны, задачи, approvals, события, находки, учёт LLM, журнал аудита и файлы артефактов.
- `DELETE /projects/{id}` (owner) удаляет данные проекта и его артефакты, а также клон, если сервер сам его создавал. Каталоги из `ALLOWED_ROOTS` не удаляются. Пока есть активные прогоны, удаление возвращает `409`. Журнал аудита сохраняется.
- Retention: `ai-coding server purge` (для cron) или автоматически раз в час в worker'е при `AI_CODING_SERVER_RETENTION_DAYS > 0`. Удаляются завершённые прогоны старше срока, их неиспользуемые артефакты, просроченные ключи идемпотентности и токены, отозванные или истёкшие более 30 дней назад.

## Производительность и нагрузочный тест (NFR-02)

```bash
uv run python loadtest/run_load.py --out load-out
```

Скрипт поднимает PostgreSQL (одноразовый, по digest), API (`serve --workers 2`) и два процесса worker'ов по 10 прогонов. Затем запускает k6 (образ по digest) со сценарием `loadtest/server.js`. Сценарии работают одновременно:
- поток чтения API;
- 60 discovery-прогонов от 30 пользователей, каждый опрашивается раз в секунду до завершения;
- раз в 200 мс считывается `aicoding_jobs{state="leased"}` — число прогонов, выполняющихся в этот момент на всех worker'ах.

Пороги, при нарушении которых k6 завершается с кодом 99:
- ≥ 10 одновременных прогонов;
- все прогоны завершены, неуспешных нет;
- ошибок чтения < 1 %;
- p95 чтения < 500 мс.

CI запускает тест в job «Load test (k6)», итоги попадают в summary job'а.

Измерено на 4 vCPU, где API, worker'ы, PostgreSQL и k6 работают на одной машине (S9):

| Профиль | Запросов/с | p95 / p99 чтения | Прогоны | Одновременно |
| --- | --- | --- | --- | --- |
| По умолчанию: 15 итераций чтения/с, опрос раз в 1 с | 54 | 371 / 581 мс | 60 из 60 | 20 |
| Перегрузка: 40 итераций/с, опрос каждые 250 мс (`--read-rate 40 --poll-seconds 0.25`) | 92 | 1813 / 2142 мс | 60 из 60 | 20 |

Под перегрузкой растёт задержка, но ошибок нет и все прогоны завершаются. Предел одного процесса API — около 200 запросов в секунду на ядро (≈ 4,7 мс CPU на запрос). На общей машине его делят с worker'ами, поэтому для большего трафика добавляйте процессы API и выносите worker'ы на отдельные хосты.

