# Проверка подготовки

Дата: 2026-10-06. Исторический статус подготовки: завершена. Реализация MVP выполнена позднее; текущие проверки — в [implementation-status.md](implementation-status.md).

## Выполнено

- Изучен исходный PDF, сохранён текст и составлена матрица R01–R18.
- Подготовлены specification, finance/tax rules, architecture, agent loop/grounding, ADR и decisions.
- Подготовлены 10 JSON Schemas, четыре function definitions, prompt, три scenario configs и три малых golden datasets с oracle (37 транзакций суммарно).
- Подготовлены окружение-пример, ignore files, tooling config, Makefile и проверка artifacts.
- Составлены поэтапный план на 3–5 часов, тестовая матрица и Definition of Done.

## Фактически выполненные проверки

| Проверка | Результат |
|---|---|
| `python3 scripts/check_preparation.py` | Passed: 18 JSON files, schema structures/refs, local links, fixtures arithmetic/evidence |
| `python scripts/check_preparation.py --schemas` | Passed: Draft 2020-12 meta-validation и validation пяти fixture/example inputs, с FormatChecker |
| `python -m ruff check scripts/check_preparation.py` | Passed |
| `python -m ruff format scripts/check_preparation.py` | Форматирование выполнено |
| `python -m mypy scripts/check_preparation.py` | Passed: 1 файл |

Базовая проверка использовала системный Python 3.13.5. Для JSON Schema/Ruff/mypy использованы уже установленные инструменты из локального Python-окружения соседнего проекта; они не являются зависимостью будущего приложения. Для повторной full schema validation в независимом окружении установить [requirements-preparation.txt](../requirements-preparation.txt).

Проверка арифметики относится к hand-authored fixtures. Она не доказывает корректность ещё не созданных domain tools. Полная schema validation проверяет dataset/tool-result/synthesis examples; sample complete report ещё не создавался и runtime semantic grounding ещё не выполнялся.

## Следующий этап

Пакет, полная когорта, tests и actual mock traces реализованы по [плану](implementation-plan.md). Остаются чистая установка, Docker build/run и отдельный opt-in live smoke; GitHub-публикация не выполнена. См. [текущий статус](implementation-status.md).
