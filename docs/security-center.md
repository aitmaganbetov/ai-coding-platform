# AI Security Center — изолированный модуль, этап 1

## Архитектура и границы PR

Исследован baseline `6516b5d7cf3aa8cb5ce93e57776c51384383a1ec`.
Платформа уже имеет:

| Область | Существующая точка интеграции | Основа Security Center |
| --- | --- | --- |
| SAST | `tools/semgrep.py`, `workflows/audit.py` | Покрытие сохранённых `scan.semgrep` |
| Secrets | `tools/gitleaks.py`, `security/redaction.py` | Без raw evidence, message и secret title |
| SCA | `tools/osv.py`, `tools/trivy.py`, offline DB cache | Отдельный статус каждого анализатора |
| IaC | `TrivyAdapter`: `trivy fs --scanners vuln,misconfig` | Покрытие запуска Trivy fs |
| Container | Digest-pinned executors; Trivy сейчас сканирует файловую систему | `unsupported`: запуск образа не реализован |
| DAST | Отдельного ZAP adapter нет | `unsupported`, запросов к целям нет |
| Findings | `persistence.models.Finding`, fingerprint, project/run scope | Пагинация и полные счётчики одного run |
| Compliance | Детерминированные `quality/`, SARIF/JUnit в `reports/` | `not_assessed`, без заявления о соответствии |
| AI Fix | `workflows/develop.py`, `maintain.py`, approval, guarded LLM | `disabled`, без генерации/применения изменений |

`security_center/coverage.py` — отдельная проекция результатов без I/O.
`api/security_center.py` использует существующий `ServerCore.require` с
`FINDINGS_VIEW`, cap роли токена и ограничение проекта токена. Администратор
инстанса не получает неявный доступ. Run дополнительно проверяется на принадлежность
проекту. Нижние слои не импортируют новый модуль; контракт import-linter расширен.
Новых таблиц, миграций, пакетов, workflow, фоновых задач и изменений production нет.

## Как включить только в тестовом приложении

```python
app = create_app(server, broker=None, security_center_enabled=True)
```

По умолчанию `False`: маршрут отсутствует даже в OpenAPI. В этом этапе нет
переменной окружения, CLI-переключателя и Web UI: обычный `server serve` модуль не включает.
Используйте отдельную тестовую БД и копию данных, без production credentials.

```http
GET /api/v1/projects/{project_id}/security-center/runs/{run_id}?limit=100&offset=0
Authorization: Bearer <project-scoped-token>
```

`limit`: 1–500, `offset`: 0–100000. Сортировка findings по `created_at DESC, id DESC`.
`summary.total` и `summary.by_severity` учитывают весь run, независимо от страницы.
Это число записей, включая baseline/suppressed и повторные fingerprints; оно не
означает число уникальных открытых уязвимостей. Исторические findings не объединяются
между run. При изменении данных работающего run страницы могут смещаться;
для воспроизводимого обзора используйте завершённый run.

Ответ `schema_version=1` содержит `run_state`, `coverage`, `summary`, `findings`,
`page`, `compliance` и `ai_fix`. Findings выдают только разрешённые поля: ID,
source, rule, severity, title, file_path, line, fingerprint и status.
Evidence, scanner message, raw output и произвольные metadata не выдаются.
Для Gitleaks title всегда `Secret detected`; строки дополнительно проходят общий redactor.
Redactor — эвристика; следующий этап должен отдельно определить политику чувствительных
имён файлов и расширить тесты на новые адаптеры.

`completed` означает успешное выполнение анализатора, даже если он нашёл проблемы.
Это не PASS и не compliance. Пропущенные, отсутствующие и неуспешные анализаторы
не дают полного покрытия. Только `SUCCEEDED` задачи с capability `scan.<tool>`
и согласованным output учитываются; незавершённые/отменённые задачи не подтверждают
покрытие. Ошибка завершённого tool result даёт `error`; отсутствие результата —
`incomplete`/`not_run`. SCA требует оба существующих анализатора; это консервативная
индикация, а не новая политика Quality Gates. IaC показывает выполнение Trivy,
не доказательство наличия и проверки всех IaC ресурсов.

## Безопасный поэтапный план

Каждый этап — отдельный PR и выключенный по умолчанию флаг. Включение production
не входит в этот PR. Владелец платформы согласует rollout после проверки staging.

| Этап | Результат | Критерий допуска | Откат |
| --- | --- | --- | --- |
| 1, этот PR | Read-only run API и карта покрытия | Тесты RBAC, foreign run, secret redaction, pagination, default-off; lint/types/layers | Не включать аргумент приложения; БД не менялась |
| 2 | Web UI Security Center и конфигурационный feature flag; обзор run, filters, статус сканирования, ссылки на существующие отчёты | UI показывает unknown/skipped/error; role/token tests; XSS и доступ к артефактам; пилот на staging | Выключить флаг; оставить текущие findings API |
| 3 | Версионированные scan profiles: SAST/SCA/Secrets/IaC через существующий audit DAG | Digest-pinned tools, offline DB version/freshness, timeout и resource limits; parser fixtures; ошибки fail-closed | Отключить profile; текущий audit не менять |
| 4 | Container image scan и отдельный DAST worker/ZAP adapter | Изолированные ephemeral staging targets; approval на точный target+profile+TTL; egress и SSRF tests; явные лимиты | Остановить очередь нового типа, отозвать target approval |
| 5 | Findings lifecycle: triage, assignee, suppression reason/expiry, cross-run dedup, evidence references | Отдельная миграция, audit events и idempotency; изоляция проектов; backup/restore test; старые статусы совместимы | Выключить записи; сохранить совместимую схему, миграции назад только после проверки |
| 6 | Compliance evidence matrix с pinned version требований | По каждому control: evidence/run/commit/tool DB, reviewer, time, covered/failed/not_assessed; ручные controls отдельно | Отключить matrix; findings и gates независимы |
| 7 | AI Fix: proposal → review → отдельный worktree/branch → tests/rescan → draft PR | GuardedLLM, budget, secret exclusions, untrusted finding text; patch/path validation, approval, stale-commit check; без auto-merge/deploy | Выключить AI Fix; отменить job, закрыть его PR по решению владельца |

DAST: пользователь не может передать произвольный URL непосредственно executor.
Нужен реестр разрешённых staging deployments, проверка схемы/host/port и DNS/IP
при каждом соединении и redirect, запрет metadata endpoints, loopback и private
сетей кроме явно выделенной staging-сети. Сначала ограниченный ZAP baseline,
затем отдельное согласование active scan. Baseline тоже делает HTTP-запросы и
обход страниц; он не является полностью пассивным с точки зрения нагрузки и
побочных действий приложения. Production targets запрещены в этом rollout.
Не подключать Docker socket; образы читать по digest из одобренного registry,
credentials выдавать краткосрочно, без выполнения содержимого сканируемого образа.

AI Fix не получает raw secret evidence. Findings, содержимое репозитория и ответы
сканеров — недоверенные данные, не инструкции для агента. AI не может понижать
severity, менять policy/suppression или обходить gate. Review approval привязан
к commit и patch digest; изменившийся HEAD требует новой проверки. Для secrets
нужны отзыв/ротация credentials и проверка истории, простое удаление строки не
считается устранением утечки. Повторное сканирование обязано подтвердить результат.

## Проверки

```bash
uv sync --frozen
uv run pytest tests/unit/test_security_center.py tests/integration/test_security_center.py
uv run pytest tests/unit/test_server_services.py tests/integration/test_api_insights.py tests/security/test_api_access.py
uv run ruff check .
uv run ruff format --check .
uv run mypy --platform linux
uv run lint-imports
```

Тесты модуля не запускают сканеры, worker, LLM и сетевые сканирования.
Полный существующий CI остаётся без изменений. На Windows обычный strict mypy
видит существующие Unix-only API (`O_NOFOLLOW`, `openpty` и др.); Linux platform
соответствует существующему CI type-check job.

## Стандарты и первичные источники

- [OWASP ASVS 5.0.0](https://asvs.owasp.org/) — требования и ручная/автоматическая
  проверка controls. Маппинг на конкретные IDs должен быть версионирован и проверен
  отдельно; отсутствие findings не даёт соответствия ASVS.
- [OASIS SARIF 2.1.0](https://docs.oasis-open.org/sarif/sarif/v2.1.0/os/sarif-v2.1.0-os.html)
  — использовать существующий SARIF exporter, не вводить несовместимый формат.
- [ZAP baseline](https://www.zaproxy.org/docs/docker/baseline-scan/) — spider и
  passive analysis; foundation этот сканер не устанавливает и не запускает.

Проверено по официальным источникам 2026-11-07. План не является сертификацией
или утверждением выполнения требований стандарта.
