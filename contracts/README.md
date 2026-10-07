# Контракты v1

JSON Schema Draft 2020-12. Это исходный публичный v1 контракт. Runtime Pydantic-модели и четыре tools реализованы в src/fintech_copilot; schemas проверяются на совместимость с примерами и фактическими outputs. Все object schemas запрещают дополнительные поля. `tools.json` содержит function definitions с inline argument schema для provider adapter.

| Файл | Назначение |
|---|---|
| [dataset.schema.json](dataset.schema.json) | Company, immutable snapshot, transactions, invoice/evidence metadata |
| [tool-arguments.schema.json](tool-arguments.schema.json) | Четыре обязательных scope arguments |
| [tools.json](tools.json) | Имена, descriptions, параметры model-callable функций |
| [tool-result.schema.json](tool-result.schema.json) | Union четырёх success results и typed error |
| [liquidity-data.schema.json](liquidity-data.schema.json) | Balances, months, gaps, runway, lag |
| [categorization-data.schema.json](categorization-data.schema.json) | Полная сверка дебетов по cash/OPEX buckets |
| [anomaly-data.schema.json](anomaly-data.schema.json) | Transaction/monthly/cross-border findings |
| [tax-data.schema.json](tax-data.schema.json) | Expense decisions, rules, missing inputs, nullable tax amounts |
| [fact.schema.json](fact.schema.json) | Evidence-bearing typed metric value |
| [synthesis.schema.json](synthesis.schema.json) | Model-selected findings/actions/evidence, без свободных сумм |
| [report.schema.json](report.schema.json) | Complete/partial report с исходными tool results |

## Runtime checks поверх JSON Schema

Dataset допускает `anomaly_config` с шестью серверными настройками: positive integer baseline count/lookback и Decimal string ratios/deltas. При отсутствии поля применяются defaults из [финансовой спецификации](../docs/financial-logic.md). Конфигурация хранится в snapshot, не принимается через tool arguments. Synthesis включает отдельный `stress_low_runway`; при активном tool stress flag этот finding обязателен в полном отчёте. Его evidence: stress runway, closing cash, last-month burn и stress flag.

Обязательны calendar-date parsing и period constraints; cross-field company/snapshot consistency; uniqueness IDs; Decimal parsing с запретом binary floats; positive amounts; invoice conditions; category/movement validation; month coverage; tool result IDs и tool/fact value reconciliation; finding predicates; action compatibility; four successful full-period tools для complete.

`format=date|date-time|uri` в JSON Schema проверяется при включённом FormatChecker. `$id` — относительное имя файла; offline resolver регистрирует каждую schema по basename. В CLI никакой schema reference не вызывает network fetch.

Money/ratio strings используют dot и два знака. Вычисление делений — с полной Decimal precision, display после rounding. Unit-specific constraints fact values проверяются metric registry: `HKD` — signed amount, `months|percent|days` — numeric string, `date` — ISO date, `ids` — array, `flag` — boolean, `status` — known enum. Schema-level `value` union не доказывает корректность сочетания metric/unit.

## Обязательный каталог фактов

| Tool | Метрики для отчёта/grounding |
|---|---|
| Liquidity | opening_cash, closing_cash, minimum_cash/date, operating inflows/outflows, average/last-month burn, runway/status, stress, flags; cash_gap_ids и индивидуальные start/end; payment lag |
| OPEX | known_opex_total, unknown total/IDs, inventory/non-OPEX totals; category total/share и pre-rounding concentration flag |
| Anomaly | anomaly_ids, per-anomaly amount/baseline/threshold; unknown cross-border total/IDs |
| Tax | four decision totals, per-decision IDs, review row IDs, ruleset status и rule references |

Имена per-category/interval metrics включают stable category/interval identifier. Findings связывают факт с relevant object typed data через derivation code; ссылки на фиктивные или чужие IDs отклоняются. Ruleset ID принадлежит server context. Empty outputs всё равно содержат факты нулевых агрегатов/пустых ID lists, а не исчезнувший evidence catalogue.

Полная проверка схем/примеров доступна после установки `requirements-preparation.txt`: `python3 scripts/check_preparation.py --schemas`. Базовый режим проверяет структуры и ссылки, не выполняет полную JSON Schema validation.

Runtime schemas экспортируются `scripts/export_schemas.py` в artifacts/schemas; проверяются поля/required и совместимость outputs, полного равенства схем не заявляется. Не передавать file `$ref` из checked-in result schemas провайдеру: model-callable аргументы уже inline; synthesis schema при необходимости раскрывает refs в adapter. Runtime date/Decimal behavior покрыть тестами, а не предположить из `strict=True`.
