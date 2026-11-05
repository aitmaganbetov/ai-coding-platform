# Runbook: восстановление и инциденты

Каждый раздел устроен одинаково: симптомы → диагностика → действия → проверка. Команды `docker compose` выполняются из `deploy/` с `-f docker-compose.yml`. SQL-запросы в разделах диагностики только читают данные.

Подключение к PostgreSQL:

```bash
docker compose exec postgres psql -U aicoding -d aicoding
```

## API не отвечает

**Симптомы:** UI не открывается, `/healthz` не отвечает, сработал алерт `up == 0`.

1. `docker compose ps` — запущены ли `caddy` и `api`.
2. `docker compose logs --tail 200 api` — ошибки старта: неверные переменные `AI_CODING_SERVER_*` (сообщение об ошибке называет переменную), недоступная БД.
3. `curl -s http://127.0.0.1:8000/healthz` изнутри сети compose — это отделяет проблему API от проблемы Caddy и TLS. Логи Caddy: `docker compose logs caddy` (сертификат, DNS домена).
4. Перезапуск: `docker compose up -d api`. Прогоны это не затрагивает: их выполняет worker.

**Проверка:** `/readyz` возвращает 200, вход в UI работает.

## База данных недоступна

**Симптомы:** `/readyz` возвращает 503, ответы 500, в логах `OperationalError`.

1. `docker compose ps postgres`, `docker compose logs --tail 100 postgres`.
2. `too many clients` — пул соединений больше `max_connections`. Уменьшите `AI_CODING_SERVER_DB_POOL_SIZE` / `DB_MAX_OVERFLOW` или число процессов. Правило: `процессы × (pool + overflow) < max_connections`.
3. Переполнен диск: `df -h` на томе `pgdata` → [раздел о диске](#закончилось-место-на-диске).
4. Повреждение данных → [восстановление из backup](#восстановление-из-backup).

Прогоны, прерванные во время недоступности, продолжаются сами (см. следующий раздел).

## Прогон завис или worker упал

**Симптомы:** прогон долго в состоянии `RUNNING`, растёт `aicoding_jobs{state="queued"}`.

1. Живы ли worker'ы: `docker compose ps worker`, `docker compose logs --tail 200 worker`.
2. Задания по состояниям: `SELECT state, count(*) FROM run_jobs GROUP BY state;`
3. Чьи задания захвачены и до какого момента: `SELECT run_id, lease_owner, lease_expires_at, attempts FROM run_jobs WHERE state = 'leased';`
4. Если worker упал, его lease истекает, и задание забирает другой worker: прогон продолжается с последнего checkpoint без повтора выполненных шагов (AC-04). Вмешиваться не нужно — достаточно, чтобы хотя бы один worker работал: `docker compose up -d worker`.
5. Задание в `failed` исчерпало 3 попытки. Причина — в `run_jobs.error` и в событиях прогона (`GET /api/v1/runs/{id}`). Устраните причину (образ, сеть, конфигурация проекта) и запустите прогон заново. Отмена — `POST /runs/{id}/cancel` или кнопка в UI.

**Проверка:** очередь убывает, прогон завершён.

## Песочница не работает

**Симптомы:** инструменты SKIPPED с причиной о Docker, ошибки `Docker daemon is not reachable` или `no runtime 'runsc'`.

1. На хосте worker'а: `docker info`. Для rootless Docker или удалённого демона проверьте `DOCKER_HOST` и `DOCKER_CONTEXT` в окружении worker'а.
2. Образ не найден (`not present locally`): `ai-coding tools pull`, песочница сама ничего не скачивает.
3. `no runtime 'runsc'`: на хосте не выполнен `runsc install` и перезапуск Docker, либо из `execution.docker.runtime` нужно убрать `runsc`.
4. Не удаётся установить зависимости, в stderr шага `egress denied by policy: <host>`: хост не входит в разрешённые. Добавьте его в `security.egress_allowlist` проекта, если это действительно нужный реестр, и в `install_hosts` или `performance_hosts`.

## Закончилось место на диске

1. `df -h`. Чаще всего место занимают образы инструментов (`docker system df`; образ Playwright весит 3,7 ГБ), артефакты и клоны в `AI_CODING_DATA` или WAL PostgreSQL.
2. Хранение: `ai-coding server purge` удаляет завершённые прогоны старше срока и их неиспользуемые артефакты.
3. `docker image prune` удаляет образы без тегов. Не удаляйте закреплённые образы инструментов: их вернёт только `tools pull` с доступом в сеть.
4. Временные каталоги песочницы удаляются сами. Если они остались после аварийного завершения (`ai-coding-*` в `TMPDIR`), удалите их: `docker run --rm -v <dir>:/s <образ> rm -rf /s/...`, потому что файлы принадлежат uid песочницы.

## Восстановление из backup

1. Остановите приложение, оставив БД: `docker compose stop api worker`.
2. Восстановите (копии по умолчанию в `deploy/backups/` или в каталоге, указанном `backup.sh`):

```bash
gunzip -c <каталог-копий>/aicoding-<время>.sql.gz | docker compose exec -T postgres psql -U aicoding -d aicoding
```

3. Восстановите каталог данных (`AI_CODING_DATA`) из той же точки времени, если его копия есть.
4. `ai-coding server migrate` (на случай копии от предыдущей версии), затем `docker compose up -d`.
5. Проверка: вход, список проектов, открытие артефакта последнего прогона. Прогоны, начатые после момента копии, потеряны — их нужно запустить заново.

## Откат обновления

1. `docker compose stop api worker`.
2. Если новая версия добавила миграции (см. `CHANGELOG.md`) и откатываться нужно на версию без них, восстановите копию, сделанную перед обновлением ([раздел выше](#восстановление-из-backup)). Не запускайте старую версию на схеме новой.
3. Верните прежний образ по digest в compose и выполните `docker compose up -d`.

## Скомпрометирован токен или ключ

| Что | Действия |
| --- | --- |
| Токен API (`aic_…`) | `ai-coding server token revoke <id>`: действует сразу. Посмотрите журнал аудита по этому actor'у с момента утечки |
| Пароль пользователя | `ai-coding server user password <имя>` и отзыв его токенов (`GET /api/v1/tokens`, `DELETE`) |
| `AI_CODING_SERVER_METRICS_TOKEN` | Новое значение в `.env`, `docker compose up -d api`, обновить конфигурацию Prometheus |
| Пароль PostgreSQL | `ALTER USER aicoding PASSWORD '…'`, новое значение в `.env`, `docker compose up -d api worker` |
| Ключи S3, LLM, GitHub | Отозвать у поставщика, выпустить новые, обновить окружение worker'а и API, перезапустить. В БД и коде их нет |

Затем выгрузите журнал аудита: `GET /api/v1/audit-events` от администратора — в нём серверные события и попытки доступа к чужим проектам. Найдите действия злоумышленника и при необходимости экспортируйте затронутые проекты для анализа.

## Подозрение на выход из песочницы

1. Изолируйте хост worker'а: остановите worker и отключите его от сети. API и данные на других хостах не затронуты, потому что API к Docker доступа не имеет.
2. Сохраните доказательства: журнал аудита прогона (`GET /api/v1/runs/{id}`, `audit-events`), логи worker'а и Docker, список контейнеров `docker ps -a --filter label=ai-coding.egress`.
3. Отзовите ключи, к которым имел доступ worker (S3, LLM, GitHub, PostgreSQL).
4. Пересоздайте хост worker'а с gVisor (`execution.docker.runtime: runsc`) и в rootless-режиме. Проверьте результат набором проб: `uv run pytest tests/security/test_sandbox_escape.py tests/integration/test_gvisor.py`.

## Запрос на удаление данных

1. Сначала экспорт, если он нужен владельцу: `GET /api/v1/projects/{id}/export`.
2. Удаление: `DELETE /api/v1/projects/{id}` (роль owner). Пока у проекта есть активные прогоны, ответ — 409: сначала отмените их. Удаляются данные проекта, артефакты и созданный сервером клон. Журнал аудита остаётся как доказательство действий.
3. Хранение по сроку применяется автоматически (`AI_CODING_SERVER_RETENTION_DAYS`) или командой `ai-coding server purge`.
