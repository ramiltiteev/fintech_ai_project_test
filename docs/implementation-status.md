# Состояние реализации

Дата: 2026-10-06. Offline MVP реализован; окончательная проверка установки и контейнера остаётся открытой.

## Реализовано

- Immutable Pydantic v2 snapshots, строгие деньги Decimal/JSON strings и календарные периоды.
- Seeded генератор трёх компаний: 90/120/150 транзакций за июль–сентябрь 2026. Golden-fixtures не изменялись.
- Четыре финансовых/налоговых tools, whitelist dispatcher, typed results, детерминированные result/fact IDs и provenance.
- Многошаговый mock проходит настоящий цикл provider → validation → dispatcher → domain tools → matching result messages → synthesis.
- Grounding проверяет полный scope, result/fact/transaction references, соответствие facts данным, finding predicates и actions. Свободные числа и prose в synthesis запрещены.
- Лимиты ходов/вызовов/deadline, одна transient retry, одна synthesis repair, partial reports и безопасные ошибки.
- Optional LiteLLM adapter сохраняет исходное assistant message, включая дополнительные provider fields; credentials только из окружения. Префиксы конфигурации: openai/, anthropic/, gemini/. Работа конкретных live-моделей пока не подтверждена.
- CLI всех профилей, JSON/Markdown/JSONL, summary, exit codes 0/2/3, экспорт runtime schemas, Dockerfile с непривилегированным пользователем.

## Фактически выполненные проверки

| Проверка | Результат |
|---|---|
| `python3 scripts/check_preparation.py` | Passed |
| `.venv/bin/python scripts/check_preparation.py --schemas` | Passed: исходные схемы и фикстуры |
| `.venv/bin/python -m pytest -q` | 67 passed; 1 live test deselected |
| `.venv/bin/python -m ruff check .` | Passed |
| `.venv/bin/python -m ruff format --check .` | Passed |
| `.venv/bin/python -m mypy src` | Passed |
| `.venv/bin/python scripts/export_schemas.py` | 10 runtime schemas exported |
| CLI all/mock/seed 42 | Все три отчёта complete; dataset/report прошли исходные JSON Schemas |
| Повтор seed 42 | Равны datasets, canonical reports и Markdown; исключены run_id/generated_at |
| Live adapter protocol | Offline transport fixture; реальные финансовые tools, API не вызывался |
| Состав будущего коммита | PDF, `docs/source/`, окружение, caches, artifacts и локальные настройки исключены; совпадений проверенных secret patterns нет |
| Копия только Git-кандидатов | Preparation/schema/link checks прошли без локального PDF, `docs/source/`, `.venv` и artifacts; чистая установка зависимостей при этом не проверялась |

Среда: Python 3.13.5, Pydantic 2.13.5, pytest 8.4.2, Ruff 0.16.7, mypy 1.20.2, jsonschema 4.26.0. Проверенные версии и dependency closures записаны в `requirements-runtime.txt` и `requirements-dev.txt`. Python 3.11/3.12 отдельно не проверялись.

Локальная `.venv` создана без pip; зависимости скопированы из уже существующего Python-окружения, код подключён через локальный `.pth`. Тесты затем выполнены локальным `.venv/bin/python`. Это проверяет приложение, но не доказывает чистую установку, wheel build или загрузку зависимостей с PyPI.

## Открытые пункты

- Чистая установка и сборка wheel не выполнялись: системный Python не имеет pip/ensurepip, сетевой доступ к PyPI отсутствует. Сценарий проверки — [setup.md](setup.md).
- Docker build/run не выполнялись: доступ к `/var/run/docker.sock` запрещён. Образ и установка внутри него не проверены.
- LiteLLM не установлен в проверенном окружении. Реальный SDK/live API и native Tool Calling выбранной модели не проверены; SDK range не является проверенным live lockfile.
- Git remote настроен пользователем. Локальный коммит запрошен, но `git add` заблокирован окружением: `.git` доступен только для чтения, невозможно создать `index.lock`. Коммит и публикация не выполнялись.

Mock-демо: `artifacts/demo/`; runtime schemas: `artifacts/schemas/`. Они игнорируются Git. README содержит фактическую строку mock trace; она не является свидетельством live-прогона.

Cash ledger не является двойной бухгалтерской записью. Runway ретроспективный; нет прогноза открытых AP/AR, FX, начислений и audited accounts. Налоговый инструмент создаёт expense review schedule: `filing_ready=false`, `tax_payable=null`, `assessable_profit=null`. IRD rule URLs и versioned ruleset находятся в domain tax module, а не в prompts.
