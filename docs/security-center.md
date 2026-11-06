# AI Security Center

Отдельный модуль платформы: покрытие сканирования, находки SAST, SCA, секретов и IaC одного
run'а. Модуль **выключен по умолчанию** и **только читает** данные: не запускает сканеры, не
обращается к сетевым целям, не вызывает LLM, ничего не записывает.

Готово: этап 1 (read-only API) и этап 2 (Web UI и серверный флаг). План остальных этапов —
в разделе [«Поэтапный план»](#поэтапный-план).

## Архитектура

Исследован baseline `6516b5d7cf3aa8cb5ce93e57776c51384383a1ec`. Модуль переиспользует то, что
в платформе уже есть:

| Область | Существующая точка интеграции | Что показывает Security Center |
| --- | --- | --- |
| SAST | `tools/semgrep.py`, `workflows/audit.py` | Покрытие по задаче `scan.semgrep` |
| Secrets | `tools/gitleaks.py`, `security/redaction.py` | Покрытие; находки без заголовка, сообщения и evidence |
| SCA | `tools/osv.py`, `tools/trivy.py`, офлайн-базы | Отдельный статус каждого анализатора; нужны оба |
| IaC | `TrivyAdapter`: `trivy fs --scanners vuln,misconfig` | Выполнение Trivy fs |
| Container | Trivy сейчас сканирует файловую систему | `unsupported`: сканирование образов не реализовано |
| DAST | ZAP-адаптера нет | `unsupported`: запросов к приложениям нет |
| Findings | `persistence.models.Finding`, fingerprint, project/run scope | Счётчики run'а, фильтры, пагинация, детали по allowlist |
| Quality Gates | Результат задачи `quality.gates` | Только `result` и `blocking` gate `security` |
| Compliance | Детерминированные `quality/`, SARIF/JUnit в `reports/` | `not_assessed`, без заявления о соответствии |
| AI Fix | `workflows/develop.py`, `maintain.py`, approval, GuardedLLM | `disabled`, без генерации и применения изменений |

Код:

- `security_center/coverage.py` — проекция задач `scan.<tool>` в состояния покрытия, без I/O;
- `security_center/findings.py` — безопасное представление находки (allowlist полей, redactor);
- `api/security_center.py` — запросы к БД с проверкой прав, без записи;
- `api/app.py` — маршруты (подключаются только при включённом флаге), `features` в `GET /me`;
- `web/src/pages/SecurityCenter.tsx` — раздел Web UI.

Нижние слои модуль не импортируют: `ai_coding.security_center` — отдельный слой в контракте
import-linter. Новых таблиц, миграций, зависимостей, фоновых задач и сетевых обращений нет.

## Настройка

| Переменная | По умолчанию | Значения |
| --- | --- | --- |
| `AI_CODING_SERVER_SECURITY_CENTER` | `off` | `on`/`true`/`1`/`yes` — включить; `off`/`false`/`0`/`no` или пусто — выключить |

Любое другое значение (например, `enabled`) **останавливает запуск сервера**, а не включает
или выключает модуль молча.

Пока флаг выключен:

- маршрутов `…/security-center/…` нет, они не попадают даже в OpenAPI, любой запрос получает `404`;
- `GET /me` возвращает `"features": {"security_center": false}`;
- пункта Security Center нет ни в меню, ни в командной палитре; прямой адрес `#/security`
  показывает «Модуль выключен на этом сервере» и ничего не запрашивает у API.

docker-compose передаёт переменную во все сервисы платформы со значением `off`. Чтобы включить
модуль, добавьте в `deploy/.env` строку `AI_CODING_SERVER_SECURITY_CENTER=on` и перезапустите
сервис `api`. Флаг читается при запуске процесса. Для тестов есть аргумент
`create_app(server, security_center_enabled=True)`; явное значение важнее настройки.

## Использование

**Web UI**, раздел «Качество → Security Center» (`#/security`, можно передать
`?project=<id>&run=<id>`):

1. Выберите проект (доступны проекты, где у вас есть право `findings.view`) и run (по умолчанию — последний).
2. **Покрытие.** Карточки SAST, SCA, «Секреты», IaC, «Контейнеры», DAST с состояниями:

   | Состояние категории | Значение |
   | --- | --- |
   | `completed` — «Выполнено» | Все анализаторы категории отработали. Это **не PASS**: находок может быть много |
   | `incomplete` — «Неполное» | Часть анализаторов не запускалась, ещё выполняется или отменена |
   | `skipped` — «Пропущено» | Ни один анализатор не выполнялся (например, нет офлайн-базы) |
   | `error` — «Ошибка» | Анализатор завершился ошибкой, задача упала или результат некорректен |
   | `unsupported` — «Не реализовано» | В платформе нет такого анализатора (DAST, контейнеры) |

   По каждому инструменту показаны состояние (`completed`, `skipped`, `error`, `running`,
   `not_run`) и причина в виде фиксированного кода (`timeout`, `offline_db_missing`,
   `parse_error`, `task_failed`, `not_planned`…). Текст ошибки инструмента может содержать пути,
   stderr или текст advisory, поэтому он остаётся на сервере.
3. **Quality Gate `security`**: результат и признак блокировки. Причину и доказательства
   смотрите на странице run'а (нужно право `run.view`). **Compliance** — «не оценивалось»,
   **AI Fix** — «выключено».
4. **Severity** — счётчики по всему run'у. Сюда входят подавленные baseline записи и повторы:
   это число записей, а не уникальных уязвимостей.
5. **Находки** — таблица по 50 строк с фильтрами по важности, инструменту и статусу.
   Фильтрация и пагинация выполняются на сервере. Нажатие на строку открывает детали.
   Для SCA показываются пакет, установленная версия, версия с исправлением, экосистема, CVE/GHSA и
   ссылка `https`; для IaC — рекомендация сканера; для SAST — CWE. Сообщения сканера, фрагменты
   кода и raw evidence не выдаются никогда, у секретов нет ни значения, ни заголовка.

Пока run выполняется, страница обновляется каждые 5 с и предупреждает, что данные неполные.
Пустая таблица сопровождается напоминанием: отсутствие находок не означает безопасность.

**API** (`schema_version: "2"`), оба маршрута — только `GET`:

```http
GET /api/v1/projects/{project_id}/security-center/runs/{run_id}?limit=50&offset=0&severity=high&source=semgrep&status=open
GET /api/v1/projects/{project_id}/security-center/runs/{run_id}/findings/{finding_id}
Authorization: Bearer <token>
```

| Параметр | Ограничения (иначе `422`) |
| --- | --- |
| `limit` / `offset` | 1–500 / 0–100000 |
| `severity` | `critical`, `high`, `medium`, `low`, `info` |
| `source` | `^[a-z0-9][a-z0-9._-]{0,99}$` (имя инструмента) |
| `status` | `^[a-z][a-z_-]{0,19}$` |

Ответ обзора: `run` (state, finished, mode, kind, время), `coverage`, `security_gate`,
`summary` (total, by_severity, by_source, by_status — за весь run, без учёта фильтра),
`findings` (страница), `page` (limit, offset, total с учётом фильтра, filters), `capabilities`,
`compliance`, `ai_fix`. Сортировка: `created_at DESC, id DESC`.

**Права.** Каждый запрос проверяет `findings.view` в проекте на сервере. Читают owner,
maintainer, developer, auditor и agent-токен своего проекта. Роль ограничивается `max_role`
токена, токен проекта не видит другие проекты. Администратор инстанса без членства в проекте
получает `404`. Run и находка должны принадлежать проекту из пути, иначе `404`. Методов
записи нет (`405`).

## Ограничения

- Модуль показывает только то, что уже записали прогоны `audit` (и другие run'ы с задачами
  `scan.*`). Запускать сканирование из раздела нельзя: это делается в Audit Center.
- DAST и сканирование образов контейнеров не реализованы и обозначены как `unsupported`.
- Compliance не оценивается, AI Fix выключен.
- Покрытие означает выполнение анализатора, а не полноту проверки: IaC показывает работу Trivy
  fs, а не то, что проверены все IaC-ресурсы. SCA требует обоих анализаторов — это консервативная
  индикация, а не новая политика Quality Gates.
- В БД у находок нет статусов baseline: они есть только в отчёте аудита. Фильтр по статусу видит
  значения из БД (обычно `open`).
- Заголовки SAST/SCA/IaC и пути файлов проходят redactor; это эвристика. Заголовок Semgrep строится
  из сообщения правила и может содержать подставленные метапеременные. У находок Gitleaks и
  `trivy` класса `secret` заголовок всегда `Secret detected`. Политику чувствительных имён
  файлов нужно определить отдельно.
- Находки разных run'ов не объединяются. Пока run выполняется, страницы могут смещаться; для
  воспроизводимого обзора используйте завершённый run.

## Проверка на staging

Production не включать, пока не пройдены шаги 1–6 на staging с отдельной БД и копией данных без
production credentials.

1. Разверните ветку на staging с `AI_CODING_SERVER_SECURITY_CENTER=off`. Проверьте выключенное
   состояние (меню, `404` API, сообщение раздела):
   `node web/e2e/security.mjs https://staging.example.org USER PASSWORD off`
2. Включите `AI_CODING_SERVER_SECURITY_CENTER=on` и перезапустите `api`.
3. Запустите аудит пилотного проекта в Audit Center (обычный путь, approvals и Quality Gates не
   меняются).
4. Пользователем-аудитором этого проекта выполните
   `node web/e2e/security.mjs https://staging.example.org AUDITOR PASSWORD on /tmp/shots`. Проверка
   смотрит ширины 320–1920 px, переполнение, ошибки консоли (включая CSP), карточки покрытия,
   детали находки и то, что ни одна строка сканера не выполнилась как HTML.
5. Вручную: пользователь без членства и администратор без членства получают «Run не найден…»;
   токен другого проекта — `404`; состояния карточек совпадают со страницей run'а и
   `audit-report.md`; в деталях находки Gitleaks нет значения секрета.
6. Сравните `summary.by_severity` с `GET /api/v1/findings?run_id=…` (тот же run).

Скриптам нужен пакет `playwright`, например: `npm i --no-save playwright && npx playwright
install chromium`. Проверка только читает данные и ничего не запускает.

## Откат

1. Задайте `AI_CODING_SERVER_SECURITY_CENTER=off` (или удалите строку из `deploy/.env`) и
   перезапустите сервис `api`.
2. Проверьте: `node web/e2e/security.mjs <URL> USER PASSWORD off`.

БД, миграции, данные run'ов и находок модуль не меняет, поэтому откатывать нечего, кроме флага.
Существующие Audit Center, `GET /findings` и отчёты аудита работают независимо от флага. Если
нужно убрать и код, откатите коммиты PR: другие модули от него не зависят.

## Поэтапный план

Каждый этап — отдельный PR и выключенный по умолчанию флаг. Включение в production не входит
ни в один из этих PR. Владелец платформы согласует rollout после проверки на staging.

| Этап | Результат | Критерий допуска | Откат |
| --- | --- | --- | --- |
| 1 ✅ | Read-only run API и карта покрытия | Тесты RBAC, foreign run, secret redaction, pagination, default-off; lint/types/layers | Не включать модуль; БД не менялась |
| 2 ✅ | Web UI и серверный флаг; обзор run'а, фильтры, состояние сканирования, ссылка на run | UI показывает incomplete/skipped/error/unsupported; тесты ролей и токенов, XSS, отсутствие секретов; e2e on/off; пилот на staging | `AI_CODING_SERVER_SECURITY_CENTER=off` |
| 3 | Версионированные scan profiles: SAST/SCA/Secrets/IaC через существующий audit DAG | Digest-pinned tools, версия и свежесть офлайн-БД, timeout и лимиты ресурсов; parser fixtures; ошибки fail-closed | Отключить profile; текущий audit не менять |
| 4 | Container image scan и отдельный DAST worker/ZAP adapter | Изолированные эфемерные staging-цели; approval на точные target+profile+TTL; тесты egress и SSRF; явные лимиты | Остановить очередь нового типа, отозвать approval цели |
| 5 | Findings lifecycle: triage, assignee, suppression reason/expiry, cross-run dedup, evidence references | Отдельная миграция, audit events и idempotency; изоляция проектов; тест backup/restore; совместимость старых статусов | Выключить запись; схема совместима, обратные миграции только после проверки |
| 6 | Compliance evidence matrix с закреплённой версией требований | По каждому control: evidence/run/commit/версия БД инструмента, reviewer, время, covered/failed/not_assessed; ручные controls отдельно | Отключить matrix; findings и gates независимы |
| 7 | AI Fix: proposal → review → отдельный worktree/branch → tests/rescan → draft PR | GuardedLLM, бюджет, исключение секретов, текст находок — недоверенные данные; проверка patch/path, approval, проверка устаревшего коммита; без auto-merge и deploy | Выключить AI Fix; отменить job, закрыть его PR по решению владельца |

**DAST.** Пользователь не может передать executor'у произвольный URL. Нужны:
- реестр разрешённых staging-развёртываний;
- проверка схемы, host и port, а также DNS/IP при каждом соединении и redirect;
- запрет metadata endpoints, loopback и частных сетей, кроме явно выделенной staging-сети.

Сначала ограниченный ZAP baseline, затем отдельное согласование active scan. Baseline тоже
делает HTTP-запросы и обходит страницы: он не полностью пассивен с точки зрения нагрузки и
побочных действий приложения. Production-цели в этом rollout запрещены. Docker socket не
подключать, образы брать по digest из одобренного registry, credentials выдавать краткосрочные,
содержимое сканируемого образа не выполнять.

**AI Fix** не получает raw secret evidence. Находки, содержимое репозитория и ответы сканеров —
недоверенные данные, а не инструкции для агента. AI не может понижать severity, менять policy или
suppression и обходить gate. Approval review привязан к коммиту и digest патча: изменившийся HEAD
требует новой проверки. Для секретов нужны отзыв или ротация credentials и проверка истории:
простое удаление строки не устраняет утечку. Результат подтверждается повторным сканированием.

## Проверки

```bash
uv sync --frozen
uv run pytest tests/unit/test_security_center.py tests/integration/test_security_center.py
uv run pytest tests/unit/test_server_services.py tests/integration/test_api_insights.py tests/security/test_api_access.py
uv run ruff check . && uv run ruff format --check .
uv run mypy --platform linux
uv run lint-imports
cd web && npm ci && npm run typecheck && npx vitest run && npm run build
```

Тесты модуля не запускают сканеры, worker, LLM и сетевые сканирования. Полный CI не изменён. На
Windows обычный strict mypy видит существующие Unix-only API (`O_NOFOLLOW`, `openpty` и др.);
`--platform linux` соответствует job type-check в CI.

## Стандарты и первичные источники

- [OWASP ASVS 5.0.0](https://asvs.owasp.org/) — требования и ручная/автоматическая проверка
  controls. Маппинг на конкретные ID нужно версионировать и проверять отдельно; отсутствие
  находок не даёт соответствия ASVS.
- [OASIS SARIF 2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/os/sarif-v2.1.0-os.html) —
  использовать существующий SARIF-экспортёр, не вводить несовместимый формат.
- [ZAP baseline](https://www.zaproxy.org/docs/docker/baseline-scan/) — spider и пассивный
  анализ; модуль этот сканер не устанавливает и не запускает.

Проверено по официальным источникам 2026-11-07. План не является сертификацией или заявлением о
выполнении требований стандарта.
