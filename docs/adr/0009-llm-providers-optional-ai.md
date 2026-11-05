# ADR-0009: Адаптеры LLM; AI опционален

- Статус: Принято; порт `LLMProvider`, `GuardedLLM`, учёт `model_usage` и бюджеты — реализованы в S2; адаптеры провайдеров — S5.
- Дата: 2026-11-05

## Контекст
ТЗ: OpenAI, Anthropic, Ollama/vLLM; configurable models, token limits, fallback и отключаемый AI. Наличие LLM не обязательно для детерминированных проверок. FR-12 offline с локальной моделью; FR-15 бюджеты и маршрутизация; FR-18 маскировка секретов перед внешними LLM.

## Решение
- Порт `LLMProvider`: `complete(request) -> response` со структурированным выводом (JSON Schema), подсчётом токенов и стоимости.
- Адаптеры: `OpenAIProvider`, `AnthropicProvider` (официальные SDK), `OllamaProvider`, `VLLMProvider` (OpenAI-совместимый HTTP API, `base_url`).
- `ProviderName.NONE` — AI выключен; агенты, требующие LLM, получают статус SKIPPED, детерминированные шаги выполняются.
- Перед каждым вызовом **внешнего** провайдера: Policy Engine проверяет `security.network`/`egress_allowlist`, approval `external_llm` (если требуется), Context Manager применяет redaction и фильтрует пути (`.env`, ключи, `secrets/`).
- Бюджеты: `budgets.max_tokens_per_run`, `max_cost_usd_per_run`; учёт в `model_usage`; превышение → остановка с FAILED/PAUSED.
- Маршрутизация: `ai.routing[agent]`, цепочка `ai.fallback` при ошибках/лимитах; все провайдеры цепочки проходят ту же egress-проверку (валидируется уже в конфигурации).
- Ключи — только из окружения/secret store по имени (`ai.api_key_env`).

## Последствия
Можно работать полностью offline (Ollama + `local-only`, AC-05). Добавление провайдера — новый адаптер + entry point.

## Альтернативы
LiteLLM как единый слой — отклонено для ядра (большая поверхность зависимостей, сложнее аудит egress); может быть добавлен как адаптер.
