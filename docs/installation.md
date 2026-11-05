# Установка

## Требования

| Компонент | Версия | Обязательно |
| --- | --- | --- |
| Python | 3.12 или 3.13 | да |
| Git | ≥ 2.30 | да для worktree-изоляции и DEVELOP/MODERNIZE; `init` и `config validate` работают без git |
| uv | ≥ 0.5 | рекомендуется (воспроизводимая установка по `uv.lock`) |
| Docker | ≥ 24 | с S3, для Docker sandbox executor |

Поддерживаемые ОС: Linux, macOS, Windows (NFR-01). CI проверяет все три.

## Установка из исходников (рекомендуется на этапе S1)

```bash
git clone https://github.com/aitmaganbetov/ai-coding-platform.git
```

```bash
cd ai-coding-platform
```

```bash
uv sync --frozen
```

```bash
uv run ai-coding --help
```

Без uv (pip, виртуальное окружение):

```bash
python -m venv .venv
```

```bash
source .venv/bin/activate
```

(Windows PowerShell: `.venv\Scripts\Activate.ps1`)

```bash
pip install .
```

```bash
ai-coding --version
```

Установка как изолированного CLI-инструмента:

```bash
uv tool install git+https://github.com/aitmaganbetov/ai-coding-platform.git
```

## Первый запуск

```bash
cd /path/to/your/project
```

```bash
ai-coding init --dry-run
```

```bash
ai-coding init
```

```bash
ai-coding config validate
```

`init` создаёт `.ai-coding.yml` с безопасными значениями по умолчанию: AI выключен, сеть `local-only`, исполнение локальное без команд на хосте, все Quality Gates блокирующие. Подробности — [configuration.md](configuration.md).

## Подключение LLM (опционально)

Ключ **никогда** не пишется в `.ai-coding.yml` — только имя переменной окружения:

```yaml
ai:
  enabled: true
  provider: anthropic
  model: claude-sonnet-5-5
  api_key_env: ANTHROPIC_API_KEY
security:
  network: allowlist
  egress_allowlist: [api.anthropic.com]
```

Полностью offline — локальная модель:

```yaml
ai:
  enabled: true
  provider: ollama
  model: qwen2.5-coder
  base_url: http://localhost:11434
security:
  network: local-only
```

> В S1 конфигурация провайдеров валидируется; вызовы моделей появятся в S5 (см. [backlog](backlog.md)).
