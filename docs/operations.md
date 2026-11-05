# Эксплуатационная инструкция AI Coding Platform v2.1

Для администраторов Self-Hosted-установки. Как устроен сервер и как его развернуть — в [server.md](server.md). Действия при сбоях — в [runbook.md](runbook.md).

## 1. Перед вводом в эксплуатацию

| Проверка | Как |
| --- | --- |
| Подлинность релиза | § 8: подпись образа, attestations, контрольные суммы |
| TLS | Caddy получает сертификат для `AI_CODING_DOMAIN`; `curl -sI https://<домен>/healthz` возвращает `200` и `Strict-Transport-Security` |
| Секреты не в репозитории | `deploy/.env` с правами `0600`, вне git; `POSTGRES_PASSWORD` и `AI_CODING_SERVER_METRICS_TOKEN` — длинные случайные строки |
| Изоляция worker'а | Отдельный хост или VM. Рекомендуется rootless Docker или удалённый демон ([server.md](server.md#изоляция-workerа-rootless-docker-и-удалённый-демон)) и gVisor (`execution.docker.runtime: runsc`, § 6) |
| Сеть песочницы | По умолчанию сети нет. `execution.docker.network: bridge` включайте только при необходимости: выход всё равно идёт через allowlist-прокси (§ 6) |
| Backup | `deploy/backup.sh` в cron; восстановление проверено на копии (§ 5) |
| Мониторинг | Prometheus собирает `/metrics` с токеном, алерты из § 4 настроены |
| Retention | `AI_CODING_SERVER_RETENTION_DAYS` задан по политике организации (NFR-08) |

## 2. Повседневные операции

| Задача | Команда |
| --- | --- |
| Пользователь | `ai-coding server user add NAME [--admin] --password-stdin` |
| Сменить пароль | `ai-coding server user password NAME --password-stdin` |
| Токен для CI или агента | `ai-coding server token create NAME --name ci --project <id> --role developer --ttl-hours 720` |
| Отозвать токен | `ai-coding server token revoke <token-id>` (действует сразу) |
| Действующие настройки | `ai-coding server settings` (секреты скрыты) |
| Retention вручную | `ai-coding server purge` (для cron, если `RETENTION_DAYS=0`) |
| Экспорт проекта | `ai-coding server export <project> --user <owner> -o project.zip` |

Роли назначает owner проекта в Web UI или через `PUT /api/v1/projects/{id}/members/{user}`. Права ролей описаны в [server.md](server.md#роли-и-изоляция-проектов).

## 3. Обновление версии

1. Прочитайте `CHANGELOG.md`: новые миграции, переменные и изменения поведения.
2. Сделайте backup (§ 5).
3. Проверьте подлинность нового образа (§ 8).
4. Обновите образ в compose. Схема БД обновляется при старте `serve` и `worker`; явно — `ai-coding server migrate`. Миграции идемпотентны.
5. Перезапустите сервисы: `docker compose up -d`. Worker по `SIGTERM` дожидается текущих прогонов. Прерванные продолжаются с checkpoint после старта нового worker'а.
6. Проверьте `/readyz`, вход в UI и тестовый discovery-прогон.

Откат описан в [runbook](runbook.md#откат-обновления). Миграции v2.1 (`0002_server`, `0003_rate_limits`) только добавляют таблицы и колонку и откатываются через `alembic downgrade`.

## 4. Мониторинг

`/metrics` (Prometheus) запрашивается с заголовком `Authorization: Bearer $AI_CODING_SERVER_METRICS_TOKEN`. Рекомендуемые алерты:

| Алерт | Условие (PromQL, пример) | Что означает |
| --- | --- | --- |
| API недоступен | `up{job="ai-coding"} == 0` 2 мин | процесс или прокси упал → [runbook: API](runbook.md#api-не-отвечает) |
| БД недоступна | `/readyz` ≠ 200 | → [runbook: БД](runbook.md#база-данных-недоступна) |
| Очередь растёт | `aicoding_jobs{state="queued"} > 50` 15 мин | worker'ов мало или они не работают |
| Задания падают | `increase(aicoding_jobs{state="failed"}[1h]) > 0` | задание исчерпало 3 попытки |
| Ошибки API | `sum(rate(aicoding_http_requests_total{status=~"5.."}[5m])) > 0` | дефект или недоступность БД |
| Задержка API | `histogram_quantile(0.95, sum by (le) (rate(aicoding_http_request_duration_seconds_bucket[5m]))) > 0.5` | перегрузка (§ 7) |
| Отказы rate limit | доля `status="429"` > 5 % | лимит мал для клиентов или подбор паролей |

Логи — JSON в stderr, секреты маскируются (в том числе токены `aic_…`). Журнал аудита (кто, что, проект, прогон, время, результат) доступен в UI и через `GET /api/v1/audit-events`. Попытки доступа к чужим проектам видны администратору как серверные события.

## 5. Резервное копирование и восстановление

```bash
deploy/backup.sh /var/backups/ai-coding 14
```

Скрипт делает `pg_dump` в gzip с правами `0600` и удаляет копии старше 14 дней; запускайте его из cron. Каталог данных (`AI_CODING_DATA`) копируйте средствами файловой системы. Артефакты в S3 сохраняются версионированием и репликацией bucket'а. Восстановление описано в [runbook](runbook.md#восстановление-из-backup). Проверяйте его на копии хотя бы раз в квартал.

## 6. Безопасность песочницы

- Код проектов выполняется только в Docker-песочнице:
  - пользователь 1000, без capabilities, `no-new-privileges`, seccomp;
  - корневая ФС и проект только для чтения, `/tmp` с `noexec`;
  - лимиты pids, памяти и CPU;
  - без сети и без Docker-сокета.

  Все эти свойства проверяет набор проб из 26 тестов (ADR-0032).
- **gVisor.** После `runsc install` на хосте worker'а задайте `execution.docker.runtime: runsc`. Код проекта тогда работает поверх ядра в пространстве пользователя, а не прямо на ядре хоста. CI проверяет песочницу на gVisor.
- **Сеть.** Шаги, которым она нужна (установка зависимостей, обновление пакетов, k6), получают изолированную сеть. Единственный выход — allowlist-прокси: только хосты реестра или тестируемой системы, без DNS, без прямых маршрутов и без частных адресов для имён реестров (ADR-0033). Отказы видны в stderr шага: `egress denied by policy: …`.
- **Локальный режим (CLI)** выполняет на хосте только явно разрешённые команды. На Linux и macOS действуют rlimits, на Windows — Job Object: память и CPU всего дерева процессов, их число, завершение всего дерева.

## 7. Производительность и масштабирование

Измерения S9 (k6, [server.md](server.md#производительность-и-нагрузочный-тест-nfr-02)): один процесс API выдерживает около 100–200 запросов в секунду на ядро. Как масштабировать:

- API: `serve --workers N`, по процессу на ядро. Rate limit общий для всех процессов при PostgreSQL;
- прогоны: `AI_CODING_SERVER_WORKERS` на процесс worker'а (по умолчанию 10, NFR-02) и дополнительные worker'ы на отдельных хостах;
- соединения с БД: `процессы × (DB_POOL_SIZE + DB_MAX_OVERFLOW) < max_connections`.

Нагрузочный тест повторяется командой `uv run python loadtest/run_load.py --out load-out`.

## 8. Проверка подлинности релиза

Релиз собирает workflow `Release` по тегу. Публикует его человек: GitHub release остаётся черновиком до одобрения в окружении `release`. Проверка:

```bash
sha256sum --check SHA256SUMS
```

```bash
gh attestation verify ai_coding_platform-2.1.0-py3-none-any.whl --repo aitmaganbetov/ai-coding-platform
```

```bash
cosign verify-blob ai_coding_platform-2.1.0-py3-none-any.whl --bundle ai_coding_platform-2.1.0-py3-none-any.whl.sigstore.json --certificate-identity-regexp '^https://github.com/aitmaganbetov/ai-coding-platform/.github/workflows/release-platform.yml@refs/tags/v' --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

```bash
cosign verify ghcr.io/aitmaganbetov/ai-coding-platform:2.1.0 --certificate-identity-regexp '^https://github.com/aitmaganbetov/ai-coding-platform/.github/workflows/release-platform.yml@refs/tags/v' --certificate-oidc-issuer https://token.actions.githubusercontent.com
```

```bash
gh attestation verify oci://ghcr.io/aitmaganbetov/ai-coding-platform:2.1.0 --repo aitmaganbetov/ai-coding-platform
```

SBOM (CycloneDX) приложены к релизу:
- `sbom-python.cdx.json` — пакет и зависимости по `uv.lock`;
- `sbom-web.cdx.json` — Web UI;
- `sbom-image.cdx.json` — образ.

Сканирование SBOM на уязвимости:

```bash
ai-coding audit --tool osv-scanner
```

или любым сканером CycloneDX.

## 9. Ротация ключей

Процедуры для токенов, паролей, токена метрик, пароля PostgreSQL, ключей S3 и LLM-провайдеров описаны в [server.md](server.md#ротация-ключей). Если ключ скомпрометирован, действуйте по [runbook](runbook.md#скомпрометирован-токен-или-ключ).
