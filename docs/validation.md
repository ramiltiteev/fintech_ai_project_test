# Проверки и известные ограничения

Проверено 2026-10-07 в подготовленном окружении: Python 3.13.5, Pydantic 2.13.5. Python 3.11/3.12 отдельно не проверялись. Команды установки — в [setup.md](setup.md).

## Локальные проверки

| Проверка | Результат |
|---|---|
| Pytest | 335 passed, 1 live test deselected |
| Ruff lint / format | Passed |
| mypy src | Passed, 29 source files |
| Preparation и исходные JSON Schemas | Passed |
| Runtime schema export | 14 схем, включая финансовые результаты и ответы чата |
| CLI all/mock/seed 42 | Три complete профиля; повторяемые данные, tools и canonical reports |
| Копия публичных исходников без локального архива | CLI all/mock работает; использованы уже установленные зависимости |
| HTTP API через ASGI | Компании, reports, Markdown, health, Swagger/OpenAPI, ошибки ввода и провайдера проверены |

В окружении с dev-зависимостями:

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src
python scripts/check_preparation.py --schemas
python scripts/export_schemas.py
```

Тесты используют независимые golden expectations, три seeded профиля и настоящий dispatcher. Проверяются Decimal/invalid money, пустые месяцы, burn/cash statuses, subperiods, end-exclusive даты, неизвестные категории, thresholds без future leakage, финансовые predicates, поддельные/устаревшие refs, partial reports и бюджеты. Mock/SDK transport fixtures проверяют протокол без сети; scripted verdicts не подтверждают качество реального semantic review.

Регрессии: average runway `3.82` при stress `2.00` сохраняет отдельное обязательное предупреждение в batch и контексте полного чата. Пропуск требует repair, повторный пропуск не публикуется; граница stress проверена до округления. Все шесть anomaly settings применяются к реальным tools, меняют baseline/thresholds и сохраняются при JSON replay; config входит в snapshot/result IDs. Golden-ожидания не изменялись. CLI mock всех трёх профилей после исправлений — complete.

## Реальные LLM-прогоны

Gateway: LiteLLM SDK 1.104.0, модель `mimo-v2.6-flash`; запросы выполнялись в явно выбранном live-режиме.

Эти live-прогоны относятся к версии до исправления stress/config. Новая версия проверена offline; новый платный прогон не выполнялся. Добавление config в hash меняет generated snapshot IDs при том же seed, поэтому старый trace иллюстрирует прежнюю версию; defaults сохраняют финансовые результаты.

| Сценарий | Подтверждённый результат |
|---|---|
| Batch Apex, CloudScale, Harbour — 2026-10-06 | Три complete отчёта, по четыре tools, schemas и grounding прошли; повтор CloudScale сохранил dataset/canonical report |
| Chat CloudScale — 2026-10-07 | Два complete полных отчёта; snapshot восстановлен по seed, четыре tools независимо пересчитаны, канонические ссылки и сохранённый Markdown совпали |

Пример фактических вызовов с параметрами — в [README](../README.md). Артефакты и полные trace сохраняются локально и исключены из Git. Подтверждённые прогоны — smoke, а не статистическая оценка надёжности модели.

## Ограничения

- Чистая установка из нового окружения и wheel install не подтверждены. Успешный запуск с уже установленными зависимостями их не заменяет.
- HTTP проверен через ASGI; сетевой TCP smoke и Docker build/run пока не подтверждены.
- Live-чат иногда возвращает INVALID_REPLY. Возможны медленные ответы и дублирование единиц в тексте. Проверка всех профилей в текущем live-диалоге остаётся открытой.
- Значения fact parts вставляет сервер из tools. Верная атрибуция, полнота и причины в свободном тексте зависят от вероятностного semantic review; абсолютной гарантии отсутствия галлюцинаций нет.
- Синтетика сохраняет заданные месячные targets и доли, содержит сценарные аномалии. Нет сезонности, реальных счетов, FX, открытых AP/AR, начислений и audited accounts. Runway ретроспективный; налоговый draft — expense review, `filing_ready=false`.

Платный live-тест выполняется отдельно после настройки Gateway:

```bash
python -m pytest -m live
```
