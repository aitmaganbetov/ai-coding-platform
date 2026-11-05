# Сопровождение (`ai-coding maintain`)

MAINTAIN превращает issues, находки аудита, уязвимые зависимости и телеметрию в план исправлений. Человек утверждает план, после чего платформа выполняет **по одной задаче за прогон**: обновление зависимости пакетным менеджером, исправление с регрессионным тестом, проверку и PR. Решения: [ADR-0029](adr/0029-maintain-and-dependency-upgrades.md), агент — [ADR-0028](adr/0028-s7-agents-and-advisory-triage.md).

## Быстрый старт

```bash
ai-coding audit -C . --backend docker -o reports/
```

```bash
ai-coding maintain -C . --report reports/audit-report.json --issue 12 --pr
```

В интерактивном режиме платформа показывает план и спрашивает решение. Без терминала прогон ждёт approval:

```bash
ai-coding run approve RUN -C .
```

```bash
ai-coding maintain --resume RUN -C . --pr
```

Следующая задача того же утверждённого плана:

```bash
ai-coding maintain --continue RUN -C . --pr
```

## Источники

| Опция | Что даёт |
| --- | --- |
| `--report FILE` | Находки (кроме принятых в baseline) и уязвимые зависимости из `audit-report.json`. Уязвимости Trivy и OSV группируются по пакету и lock-файлу, исправленные версии объединяются |
| `--issue N` (повторяемая) | Issue из GitHub (нужен токен, см. [github.md](github.md)) |
| `--issues-file FILE` | JSON-список `{id, title, body}` — для трекеров вне GitHub |
| `--telemetry FILE` | Сводка ошибок и инцидентов (текст) |

Все источники для агента недоверенные: инструкции внутри них не выполняются (AC-07).

## Что происходит

| Шаг | Что делает |
| --- | --- |
| `maintain.baseline` | Тесты базового коммита в песочнице. Красный baseline останавливает прогон (`--allow-red-baseline`, чтобы продолжить) |
| `maintain.plan` | Maintenance Agent планирует или откладывает **каждый** источник. У задачи есть приоритет, файлы, регрессионный тест и обновления (только до исправленных версий от сканера) |
| `maintain.approve` | Человек утверждает конкретную версию плана `m-<sha8>` |
| `maintain.deps` | Пакетный менеджер обновляет манифест и lock-файл на копии, в песочнице, со скриптами выключенными. Сеть только к реестру и только по политике |
| `maintain.item` | Цикл DEVELOP начинается с этих изменений. Developer добавляет регрессионный тест и, если нужно, правит код. Тесты в песочнице без сети |
| `maintain.verify` | Закоммиченное содержимое совпадает с принятым; тесты на коммите проходят |
| PR | Описание содержит задачу, источники, тест и обновления. Merge выполняет человек |

## Сеть для обновлений

Чтобы обновлять зависимости, разрешите sandbox-сеть и egress к реестрам вашего стека:

```yaml
execution:
  docker:
    network: bridge
security:
  network: allowlist
  egress_allowlist: [registry.npmjs.org, repo.packagist.org, packagist.org, api.github.com, codeload.github.com, objects.githubusercontent.com]
```

Без этого задача обновления завершается с причиной `execution.docker.network is 'none'` или `needs egress to …`. Изменение `*.lock` (например, `composer.lock`) — критичный файл и требует approval.

## Опции

| Опция | Описание |
| --- | --- |
| `--continue RUN [--item ID]` | Следующая (или указанная) задача утверждённого плана |
| `--item ID` | Выполнить именно эту задачу после утверждения |
| `--base B` | Базовая ветка (по умолчанию `project.default_branch`) |
| `--max-iterations N` | Лимит итераций цикла (1–10) |
| `--pr [-R owner/repo]` | Push ветки и PR |
| `--resume RUN` | Продолжить ожидающий прогон |
| `--dry-run` | Показать шаги, ничего не менять |
