# DEVELOP, MODERNIZE и AI review

Все три сценария работают с **существующим** проектом, где есть `.ai-coding.yml` с включённым разделом `ai` (см. [configuration.md](configuration.md)). Общее для всех сценариев:

- Код, написанный агентом, никогда не выполняется на хосте. Тесты запускаются в Docker-песочнице на копии дерева.
- Изменения коммитятся только в новую рабочую ветку. Основная ветка и ваша рабочая копия не меняются.
- Merge выполняет человек. Платформа его не делает.

Перед первым запуском скачайте образы песочницы:

```bash
ai-coding tools pull
```

Будут скачаны:

- образы анализаторов;
- `python-runtime` — `python:3.12-slim`;
- `node-runtime` — `node:22-slim`.

## DEVELOP — `ai-coding develop`

Задача текстом (первая строка — заголовок):

```bash
ai-coding develop "Add multiply function
We need multiply(a, b) next to sum()." -C ./calc
```

Задача из GitHub issue, с PR:

```bash
ai-coding develop --issue 7 --pr -C ./calc
```

Что происходит:

1. **Контекст.** Платформа выбирает релевантные файлы базового коммита. Файлы с секретами исключаются; `--file PATH` добавляет нужный файл вручную.
2. **Изменения.** Developer Agent предлагает изменения — полное содержимое файлов **вместе с тестами**. Изменение без тестов отклоняется.
3. **Тесты** запускаются в песочнице на копии с применёнными изменениями.
4. **Review.** Reviewer Agent проверяет diff (рекомендательно). Если затронуты миграции, Database Agent анализирует их.
5. **Повтор при ошибках.** Если тесты упали или есть critical/high замечания, их вывод возвращается агенту. Повторов не больше `--max-iterations` (по умолчанию 3).
6. **Коммит.** Принятый вариант коммитится в ветку `ai/<номер>-<slug>`:
   - удаления требуют approval `delete`;
   - CI и конфигурация — approval `critical_file_change`.
7. **Проверка коммита.** Файлы в коммите сверяются по хэшу, тесты запускаются ещё раз на дереве коммита.
8. **PR** (с `--pr`): approval `pr_create`, push, PR. В описании PR — сводка, тесты, замечания review и `Closes #N`.

Каждая итерация сохраняется артефактом прогона (`iteration-N.json`, `change.diff`, `implementation.json`).

| Опция | Назначение |
| --- | --- |
| `--max-iterations N` | Лимит итераций исправления (1–10) |
| `--strict` | SKIPPED-тесты (нет Docker/образа/сети для установки) не принимаются |
| `--no-review` | Без AI review (тесты по-прежнему обязательны) |
| `--branch`, `--base` | Рабочая и базовая ветки |
| `--resume RUN` | Продолжить после approval |
| `--dry-run`, `--require-approval` | План без изменений; approval на каждую защищённую операцию |

## Тесты в песочнице

Команды по умолчанию:

| Стек | Установка зависимостей | Тесты |
| --- | --- | --- |
| Python | `pip install --target /deps pytest` (+ `requirements*.txt`) | `python -m pytest -q` |
| Node.js | `npm ci --ignore-scripts`, если есть зависимости | `npm test` или `node --test` |

Установка зависимостей требует сеть. Она выполняется, только если:

- `execution.docker.network: bridge`;
- хосты (pypi.org / registry.npmjs.org) разрешены в `security`.

Иначе тесты помечаются SKIPPED с причиной. Пример настройки:

```yaml
execution:
  docker: {network: bridge}
security:
  network: allowlist
  egress_allowlist: [pypi.org, files.pythonhosted.org]
quality_gates:
  tests:
    command: "python -m pytest -q"          # или своя команда, например "python -m unittest"
    install: "python -m pip install --target /deps -r requirements.txt pytest"
    install_hosts: [pypi.org, files.pythonhosted.org]
```

Тестам, которым не нужны сторонние пакеты, сеть не нужна: например, `node --test` или `python -m unittest`.

## MODERNIZE — `ai-coding modernize`

```bash
ai-coding modernize "Convert callbacks to async/await" -C ./svc
```

```bash
ai-coding modernize --continue <RUN> -C ./svc        # следующий шаг плана
```

```bash
ai-coding modernize --rollback <RUN> --pr -C ./svc  # откат шага
```

Как это устроено:

1. **Baseline.** Сначала запускаются тесты базовой ветки. Если они красные, прогон останавливается; обойти — `--allow-red-baseline`.
2. **План.** Planner Agent анализирует влияние: какие файлы и модули затронуты, какие риски. Работа делится на шаги `M-01…`; у каждого шага есть файлы, способ проверки и способ отката.
3. **Утверждение.** Вы утверждаете конкретную версию плана (`p1-…`).
4. **Один шаг — одна ветка.** Прогон выполняет один шаг: тот же цикл, что DEVELOP, но тесты обязаны пройти (без регрессий). Затем коммит в ветку `ai/modernize-<slug>-m-NN`, проверка и, с `--pr`, PR.
5. **Следующий шаг.** `--continue RUN` берёт уже утверждённый план. Удобнее запускать его после merge предыдущего шага.
6. **Откат.**
   - Если шаг влит, `--rollback RUN` создаёт ветку `ai/revert-…` с `git revert`, а с `--pr` — PR.
   - Если шаг не влит, откат — закрыть PR и удалить ветку; команда это подскажет.

## AI review — `ai-coding review` и `ai-review.yml`

```bash
ai-coding review --base origin/main --head HEAD -C .
```

```bash
ai-coding review --base origin/main --pr 12 --comment --check     # в CI
```

- Замечания содержат file/line/severity.
- Результат **рекомендательный**: check run `neutral`. `failure` ставится только при политике `quality_gates.review.ai_review_blocking: true`; тогда и выход команды ненулевой.
- `--skip-if-disabled` завершает команду с кодом 0, если AI в проекте выключен.

Reusable workflow:

```yaml
jobs:
  ai-review:
    uses: aitmaganbetov/ai-coding-platform/.github/workflows/ai-review.yml@<sha>
    permissions: { contents: read, pull-requests: write, checks: write }
    with: { environment: ai-review }
    secrets: { llm-api-key: "${{ secrets.LLM_API_KEY }}" }
```

Ключ LLM храните в секретах environment `ai-review` и добавьте ему required reviewers. PR из форков по умолчанию не проверяются. С `allow-forks: true` они проверяются после approval environment, но комментарии и checks в PR из форка не пишутся.
