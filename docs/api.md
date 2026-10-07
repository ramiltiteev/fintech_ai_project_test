# HTTP API

FastAPI adapter вызывает настоящий generator и существующий цикл агента. Финансовые расчёты и report JSON contract общие с CLI. Это локальное demo API с ответом в том же HTTP-запросе; history, БД и authentication не реализованы. Клиент/reverse proxy должен допускать ожидание до run deadline 300 s для live.

## Запуск

Установить API dependencies в проектное окружение:

```bash
.venv/bin/python -m pip install '.[api]'
.venv/bin/python -m uvicorn fintech_copilot.api:app --host 127.0.0.1 --port 8000
```

Swagger: `http://127.0.0.1:8000/docs`. OpenAPI: `/openapi.json`.

| Route | Результат |
|---|---|
| `GET /health` | `{"status":"ok"}` без обращения к LLM |
| `GET /companies` | Три доступных Company profiles |
| `POST /reports` | Полный Report JSON с facts и tool_results |
| `POST /reports?format=markdown` | Отчёт `text/markdown` из контролируемого renderer |

Пример mock-запроса, не свидетельство live-прогона:

```bash
curl -X POST http://127.0.0.1:8000/reports \
  -H 'Content-Type: application/json' \
  -d '{"company_id":"cloudscale-ai","seed":42,"provider":"mock"}'
```

Request schema/example — в Swagger; финансовая response schema совпадает с существующим Report. `company_id` обязателен; `seed=42`, `provider=mock`, `question=null` по умолчанию. Свою строку `question` можно задать до 4096 символов; null использует вопрос компании из scenario config. Неизвестные поля/компании, отрицательные/нецелые seeds и неизвестный output format дают 422. Keys, правила расчётов, период, суммы и файловые пути в body не принимаются.

HTTP 200 означает сформированный отчёт; проверять `status=complete|partial` и `partial_reasons`. Partial report сохраняет доступные facts, без fabricated synthesis. Для Markdown проверять `X-Report-Status`; `X-Run-ID` связывает любой ответ с trace в журнале сервера. API не пишет артефакты на диск и не хранит их между запросами. CLI по-прежнему подходит для dataset/report/trace файлов.

## Live

Явный платный режим — только body `"provider":"live"`; установка SDK и Gateway configuration общие с CLI ([setup](setup.md#подключение-к-своему-litellm-gateway)). Перед запуском сервера экспортировать собственный `.env`; ключ не отправлять в HTTP-запросе:

```bash
set -a
source .env
set +a
.venv/bin/python -m uvicorn fintech_copilot.api:app --host 127.0.0.1 --port 8000
```

Сервер запускается без ключа для mock, `/health`, `/companies` и Swagger. Неполная live configuration даёт 503 `LIVE_NOT_CONFIGURED`; автоматического fallback в mock нет. Provider failure после старта run возвращает partial Report по прежним правилам агента. Safe trace (tools/args/result IDs, usage, diagnostics) журналируется без credentials и raw reasoning text.

## Docker API

API dependency extra отдельно от live SDK. Команды сборки/запуска должны быть проверены в окружении с доступом к Docker daemon:

```bash
docker build --build-arg INSTALL_API=true -t fintech-copilot:api .
docker run --rm -p 127.0.0.1:8000:8000 --entrypoint python fintech-copilot:api \
  -m uvicorn fintech_copilot.api:app --host 0.0.0.0 --port 8000
```

Для live API добавить build argument `INSTALL_LIVE=true`, а при запуске — `--env-file .env`. Ключи не нужны при сборке. Default image продолжает запускать offline CLI.
