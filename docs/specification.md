# Спецификация MVP v1

Нормативные решения проекта. Формулы — в [financial-logic.md](financial-logic.md), налоговые условия — в [tax-rules.md](tax-rules.md), JSON Schema — в [contracts](../contracts/README.md).

## Вход и генератор

`CompanyProfile`: стабильный `company_id`, имя, отрасль, `jurisdiction=HK`, `functional_currency=HKD`, operating assumptions. `Dataset`: schema version, snapshot ID, company profile, seed, период покрытия, opening balance, массив транзакций. Snapshot ID вычисляется по каноническому содержимому набора; идентичные данные дают идентичный ID.

`Transaction`: уникальный `transaction_id`, дата проводки `posted_on`, положительная сумма `amount`, `direction=credit|debit`, `movement_type=operating|financing|investing|tax|transfer`, `category`, HKD, synthetic counterparty, country code, описание, nullable invoice metadata и evidence metadata. Дебет/кредит — направление движения денег, не бухгалтерские проводки двойной записи.

Evidence metadata: `business_purpose_confirmed`, `chargeable_profits_link_confirmed`, `documentation_available`, `capital_nature`, `tax_kind=profits_tax|other|unknown|null`. Значения подтверждений `true|false|null`; `null` означает отсутствие сведений. В generated fixtures эти сведения синтетические и маркируются как таковые.

Запретить unknown fields, невалидные даты, неверную пару currency/company, нулевые/отрицательные amounts, более двух дробных знаков и дубли IDs. Направление отдельно от суммы. `unknown` category допустима; category дохода для дебета и expense category для кредита — ошибка MVP. Возвраты расходов/выручки потребуют отдельного расширения модели.

Контроль snapshot: даты транзакций внутри покрытия, месяцы покрытия полные, initial balance может быть отрицательным. Invoice поля либо оба null, либо receipt имеет дату счёта ≤ дате платежа и terms 60/90. Invoice lag metadata не создаёт будущих обязательств или открытой дебиторки.

Генерация по seed использует локальный PRNG. Время машины и live-модель не меняют dataset. Основная когорта: 90/120/150 транзакций, июль–сентябрь 2026. Все категории должны встречаться в когорте в целом; каждую компанию не нужно искусственно нагружать всеми категориями.

## Tools

Все model arguments: `company_id`, `snapshot_id`, `period_start`, `period_end`. Остаток и правила берутся из server context. Модель не может загрузить другой snapshot или подставить деньги. Все поля обязательны; диапазон `[start,end)` внутри coverage, start первого числа, end первого числа, start < end. Для итогового full report нужны четыре успешных результата за весь согласованный период; subperiod calls допустимы дополнительно.

| Tool | Result `data` (основные поля) |
|---|---|
| `CalculateLiquidityRunway` | opening/closing/minimum cash, minimum date, cash-gap intervals, operating inflows/outflows, monthly net burn, average/last-month burn, runway value/status, last-month stress runway, gross coverage, payment-lag summary |
| `CategorizeOpex` | category totals + IDs, known OPEX subtotal, unclassified operating debits + IDs, non-OPEX debits, share per known category |
| `DetectCashFlowAnomalies` | findings: ID, kind, amount, baseline, threshold, IDs, method, caveats; monthly comparisons |
| `DraftHKProfitsTax` | ruleset ID, per-expense decision/reason/source/IDs, decision totals, missing inputs, draft status |

Успешный tool result: `schema_version`, `tool_result_id`, `tool_name`, scope arguments, `status=ok`, `data`, `facts`, `warnings`. `data` проверяется отдельной Pydantic-моделью конкретного tool (не произвольным dict); Python-модели реализованы в `domain/results.py`; исходные schemas сохранены и проверяются на совместимость. Каталог `facts` — стабильный публичный интерфейс для synthesis и отчёта; его значения должны совпадать с `data`.

Fact: ID, result ID, metric code, `value` (decimal string/date/ID list/boolean/null), unit, transaction IDs, derivation code. Company/snapshot/period наследуются из результата. Aggregate evidence включает полный список затронутых IDs; balance fact также включает base opening balance как dataset evidence. Empty ID list допустим только для opening cash, пустого агрегата и статуса без движения, с соответствующим derivation code. Risk flags `low_runway` и `last_month_stress_low_runway` вычисляет tool до округления; validation не выводит их из rounded runway. Gap interval IDs и их start/end даты доступны отдельными facts; вложенные interval objects остаются в typed `data`.

Ошибка tool: те же scope/result/name, `status=error`, `error={code,message,retryable}`, без `data/facts`. Коды: `INVALID_ARGUMENTS`, `COMPANY_NOT_FOUND`, `SNAPSHOT_MISMATCH`, `INVALID_PERIOD`, `UNSUPPORTED_CURRENCY`, `INSUFFICIENT_DATA`, `INTERNAL_TOOL_ERROR`. Внутренняя ошибка не включает stack trace для модели; trace пользователя хранит безопасное описание.

## Публичный сценарий CLI

Команда: `python -m fintech_copilot demo --company apex-logistics|cloudscale-ai|harbour-trade|all --provider mock|live --seed 42 --output artifacts/demo`.

`--question` необязателен: по умолчанию вопрос из scenario config. `--provider live` требует `LLM_MODEL` и ключ выбранного провайдера. Нет ключа → понятная configuration error, без автоматической подмены live на mock. Output path задаёт только пользователь CLI; LLM не управляет путями.

Результаты на компанию: `dataset.json`, `report.json`, `report.md`, `trace.jsonl`. Коды выхода: 0 — все отчёты complete; 2 — configuration/input error; 3 — хотя бы один partial run или provider failure. При `all` обработать три независимых случая, записать summary и сообщить индивидуальные статусы.

## Итоговый отчёт

- Идентификация: версия, run ID, профиль, snapshot ID, период, seed, режим провайдера, generated timestamp (исключён из golden-comparison).
- `status=complete|partial`. Complete требует четырёх tools и валидного synthesis; partial перечисляет причины/недостающие tools.
- `facts`: каталог только успешных tool outputs; values берутся из tools без пересчёта в orchestration.
- `synthesis.insights`: ранжированные finding/action codes с evidence refs; renderer добавляет фразы из проверенных templates.
- `tax_draft`: `status=review_required`, ruleset, decision totals, missing inputs, `assessable_profit=null`, `tax_payable=null`, `filing_ready=false`.
- `limitations`: синтетика, cash basis, без открытых AP/AR, FX, audited accounts и полного basis period.
- `tool_results`: исходные успешные/ошибочные результаты для аудита; JSON и Markdown ссылаются на эти же facts/IDs.

Рекомендуемое действие — управленческий следующий шаг; не обещание экономии/эффекта. Tax review обязателен и не отменяется company-level прибыльностью. Контракт отчёта допускает `tax_draft=null` только для partial.

## Нефункциональные критерии

Offline mock: без сети, API-ключа и SDK провайдера; от генерации до файлов. Повтор с seed и config даёт одинаковые данные, tool results и canonical report, кроме runtime metadata. На 150 транзакциях достаточно in-memory обработки; не задавать непроверенный latency SLO. Лимиты agent loop и ошибки описаны в [orchestration.md](orchestration.md).

## Уточнения реализации

Unknown tool не получает `tool_result_id`: dispatcher возвращает protocol rejection с typed code `INVALID_ARGUMENTS`, tool не выполняется. Ошибки известных tools используют исходную result schema и серверный scope; сообщения не отражают произвольные невалидные аргументы.

Live конфигурация принимает `openai/`, `anthropic/`, `gemini/` prefixes. `.env` автоматически не читается. Budgets — server-owned dataclass; env overrides лимитов пока не поддерживаются. Реальный SDK/live API и чистая установка остаются непроверенными: [статус](implementation-status.md).
