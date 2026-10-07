# Установка и запуск

Нужен Python 3.11+. Проверенное окружение: Python 3.13.5 и Pydantic 2.13.5. Команды приложения проверены в подготовленном окружении; независимая установка с нуля и wheel пока не подтверждены — [результаты проверок](validation.md).

## Локальное демо без ключа

Из корня проекта:

```bash
python3 -m venv .venv
.venv/bin/python -m pip install -e .
.venv/bin/python -m fintech_copilot demo --company all --provider mock --seed 42 --output artifacts/demo
.venv/bin/python -m fintech_copilot chat --company cloudscale-ai --provider mock --seed 42
```

Компании: `apex-logistics`, `cloudscale-ai`, `harbour-trade`; `all` доступен в batch-демо. Mock использует настоящий dispatcher и финансовые инструменты, а выбор вызовов и проверка текста эмулируются. Во время mock-запуска сеть, ключ и LiteLLM SDK не нужны. Данные, JSON/Markdown и trace сохраняются в игнорируемой папке `artifacts/`.

## Подключение к своему LiteLLM Gateway

Установить SDK и создать локальную конфигурацию:

```bash
.venv/bin/python -m pip install -e '.[live]'
cp .env.example .env
```

Заполнить `.env` адресом Gateway, виртуальным ключом и разрешённым `model_name`:

```dotenv
LLM_MODEL=litellm_proxy/your-model-alias
LITELLM_PROXY_API_BASE=https://your-gateway.example/v1
LITELLM_PROXY_API_KEY=your-gateway-virtual-key
```

Модель должна поддерживать Tool Calling. Базовый URL может содержать `/v1` или префикс reverse proxy; SDK дописывает endpoint. Завершающий slash удаляется; credentials, query и fragment в URL запрещены. Ключи upstream провайдеров остаются на Gateway.

`.env` автоматически не загружается. Экспортировать собственный файл и явно выбрать платный live-режим:

```bash
set -a
. ./.env
set +a
.venv/bin/python -m fintech_copilot chat --company cloudscale-ai --provider live --seed 42
.venv/bin/python -m fintech_copilot demo --company cloudscale-ai --provider live --seed 42 --output artifacts/live
```

Отсутствующая конфигурация даёт ошибку до запуска сценария. Автоматического переключения в mock нет. Для прямого API доступны `openai/`, `anthropic/`, `gemini/` и соответствующий ключ окружения из `.env.example`. Подробности диалога — в [chat.md](chat.md).

## HTTP API и тесты

Для [FastAPI/Swagger](api.md):

```bash
.venv/bin/python -m pip install -e '.[api]'
.venv/bin/python -m uvicorn fintech_copilot.api:app --host 127.0.0.1 --port 8000
```

Для проверок установить dev-зависимости; команды и покрытие — в [validation.md](validation.md):

```bash
.venv/bin/python -m pip install -e '.[dev]'
```

## Docker — дополнительный вариант

Dockerfile запускает offline-демо от непривилегированного пользователя. Сборка и запуск образа пока не проверены:

```bash
docker build -t fintech-copilot .
docker run --rm fintech-copilot
```

`INSTALL_LIVE=true` добавляет SDK, `INSTALL_API=true` — HTTP-зависимости. Ключ передаётся только при запуске через `--env-file .env`. Для сохранения результатов можно монтировать `artifacts/`, предоставив UID 10001 право записи. Запуск API в контейнере описан в [api.md](api.md).
