# GitHub: PR, checks, reusable workflows

GitHub в платформе **опционален**. Аудит и гейты работают локально (`ai-coding audit`). Интеграция добавляет к ним:

- публикацию результатов в PR;
- создание PR под политикой;
- готовые reusable workflows для CI.

Архитектура описана в [ADR-0021](adr/0021-github-integration.md) и [ADR-0022](adr/0022-reusable-workflows.md).

## Учётные данные

Платформа ищет их в окружении в таком порядке:

| Переменная | Назначение |
| --- | --- |
| `AI_CODING_GITHUB_APP_ID`, `AI_CODING_GITHUB_APP_INSTALLATION_ID`, `AI_CODING_GITHUB_APP_PRIVATE_KEY` или `…_PRIVATE_KEY_FILE` | GitHub App (рекомендуется для организаций) |
| `AI_CODING_GITHUB_TOKEN` | Fine-grained PAT |
| `GITHUB_TOKEN` | Токен GitHub Actions |
| `GITHUB_REPOSITORY` | `owner/repo`; иначе берётся из `--repo` или git remote `origin` |
| `GITHUB_API_URL` | GitHub Enterprise Server (только https) |

Минимальные права fine-grained токена:

| Право | Для чего |
| --- | --- |
| Contents: read/write | push рабочей ветки |
| Pull requests: write | PR и комментарии |
| Checks: write | check runs |
| Actions: write | только для `GitHubActionsExecutor` |
| Administration: read | не нужно: rulesets читаются с правом read |

Токены не пишутся в конфиг и маскируются в логах и отчётах.

## Команды

```bash
ai-coding report export RUN -f sarif -o results.sarif      # json | markdown | sarif | junit
ai-coding report export -i audit-report.json -f junit -o junit.xml
```

```bash
ai-coding github comment --pr 12 --report audit-report.json     # один комментарий, обновляется
ai-coding github check --sha "$SHA" --report audit-report.json  # check run + аннотации
ai-coding github protection --branch main                       # exit 1, если защита неполная
```

### Создание PR

```bash
ai-coding pr open --branch ai/feature --title "Add feature" --body-file body.md --dry-run
```

```bash
ai-coding pr open --branch ai/feature --title "Add feature" --body-file body.md
```

```bash
ai-coding run approve RUN        # approval pr_create (Owner/Maintainer)
```

```bash
ai-coding pr resume RUN          # push → PR → проверка
```

Гарантии:

- **Push в защищённые ветки запрещён.**
- **Force-push не выполняется.**
- **Hooks не запускаются** (`--no-verify`).
- Токен передаётся git через переменные окружения, а не через URL или argv.
- Egress-политика проекта (`security.network`, `egress_allowlist`) проверяется до выхода в сеть. При `local-only` прогон завершается FAILED, ничего не отправив.
- Повторный запуск или resume не создаёт второй push и второй PR: работает журнал эффектов с пробами.
- **Платформа никогда не делает merge и не включает auto-merge.** Шаг `github.pr.verify` проверяет head SHA, base и выключенный auto-merge.
- Универсальный `ai-coding run resume` отказывается продолжать PR-прогон и подсказывает `pr resume`.

## Reusable workflows

Подключение в своём репозитории (закрепляйте `@<sha>`):

```yaml
name: CI
on: [pull_request]
permissions:
  contents: read
jobs:
  quality:
    uses: aitmaganbetov/ai-coding-platform/.github/workflows/quality.yml@<sha>
    permissions: { contents: read, pull-requests: write, checks: write }
    with: { tools: "ruff" }
  security:
    uses: aitmaganbetov/ai-coding-platform/.github/workflows/security.yml@<sha>
    permissions: { contents: read, pull-requests: write, checks: write, security-events: write }
  tests:
    uses: aitmaganbetov/ai-coding-platform/.github/workflows/test-python.yml@<sha>
    with: { version: "3.13" }
```

| Workflow | Что делает | Основные входы |
| --- | --- | --- |
| `quality.yml` | `ai-coding audit` (lint/static) → check run, PR-комментарий, JUnit/SARIF-артефакт | `working-directory`, `tools`, `strict`, `platform-spec`, `check-name`, `comment-key` |
| `security.yml` | Секреты и SAST (gitleaks, semgrep) → то же + загрузка SARIF в Code Scanning | + `upload-sarif` |
| `test-python/node/php/go/rust.yml` | Установка тулчейна, тесты, JUnit-артефакт | `working-directory`, `version`, `install-command`, `test-command` |
| `release.yml` | По тегу: сборка, SHA256SUMS, provenance-аттестация, draft-релиз; через environment с approval | `environment` |

Безопасность:

- на верхнем уровне `permissions: {}`;
- все actions закреплены по SHA;
- входы попадают в shell только через `env`;
- для PR из форков записи в PR, checks и Code Scanning пропускаются, а аудит всё равно выполняется и выставляет вердикт;
- `pull_request_target` не используется.

`ai-review.yml` появится в S6, вместе с LLM-адаптерами.

## Пилоты

`examples/pilot-python` и `examples/pilot-node` проверяются workflow `.github/workflows/pilots.yml` на каждом PR платформы. Это сквозная проверка AC-09 на двух стеках.

## GitHub Actions как исполнитель

`GitHubActionsExecutor`:

- запускает только workflow из allow-list через `workflow_dispatch` и ждёт результат;
- при таймауте отменяет прогон;
- возвращает сводку jobs и артефактов;
- может безопасно скачать артефакты.

Вызываемый workflow должен содержать в `run-name` входной параметр `correlation_id`:

```yaml
on:
  workflow_dispatch:
    inputs:
      correlation_id: { type: string, required: true }
run-name: ai-coding ${{ inputs.correlation_id }}
```
