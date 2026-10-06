# Стратегия тестирования и приёмка

Подготовка проверяется `scripts/check_preparation.py`: локальные ссылки, JSON parsing, schema ref resolution, структура fixtures и контрольная арифметика. Этот скрипт не тестирует будущий агент и не заменяет полный JSON Schema validator. После реализации использовать `jsonschema` для schema meta-validation и fixture validation, Pydantic для runtime boundaries.

## Когорта и воспроизводимость

| Test ID | Проверка | Ожидаемый результат |
|---|---|---|
| GEN-01 | Генератор по scenarios.json | 90/120/150 движений; каждый dataset покрывает 3 месяца |
| GEN-02 | Category/movement distribution и injection | Нужные категории/паттерны и известные injection IDs присутствуют |
| GEN-03 | Seed повтор, другой seed, descriptions | Один seed идентичен; другой меняет вариации, сохраняя риск-паттерны; PII нет |
| CASE-APEX | Invoice lag / EOD gap | Terms 60/90, median receipt lag согласован с metadata; ≥1 gap; fuel/subcontractor concentration |
| CASE-CLOUD | Низкий runway / outlier | finite runway < 3; injected abnormal debit обнаружен с baseline IDs |
| CASE-HARBOUR | Частота / неизвестные платежи | 150 движений; ≥3 unknown cross-border debits, все требуют проверки |

Формальный upper/lower cohort count — 50–150 из PDF; выбранные counts — проектное решение. Не требовать 50 транзакций от малых golden fixtures.

## Финансовые unit tests

| ID | Случай | Oracle / assertion |
|---|---|---|
| FIN-01 | Три golden fixtures | Точные totals/statuses из expected.json, Decimal equality |
| FIN-02 | Operating credits ≥ debits, positive cash | runway=null, not_burning; gross coverage конечна при outflow > 0 |
| FIN-03 | Zero/negative cash, positive burn | cash_depleted, runway=0; без NaN/Infinity |
| FIN-04 | Empty month, partial month, boundary date | Empty month в denominator; partial-month query rejected; end-exclusive |
| FIN-05 | Financing credit и tax debit | В cash включены, в operating burn исключены |
| FIN-06 | Несколько операций в один день / даты без платежей / subperiod | EOD gap без intraday артефакта; continuous intervals; carried opening balance |
| OPEX-01 | Все дебеты разных movement/category | Known OPEX + unknown + inventory + non-OPEX = all debits |
| OPEX-02 | Unknown cross-border и налог | Unknown не объявлен подтверждённым OPEX; налог не включён автоматически |
| OPEX-03 | Zero known OPEX | Shares=null; без деления на ноль |
| ANOM-01 | Cloud fixture | Платёж 120000, median 20000, ratio 6; March monthly jump 260000/120000 |
| ANOM-02 | Недостаточный baseline / constant baseline / before-vs-after | Warning при n<5; ratio при constant baseline работает; future leakage отсутствует |
| ANOM-03 | Unknown cross-border / duplicate signals | Review независимо от размера; стабильно dedup/sort |
| TAX-01 | Harbour fixture | potentially=12000, non_deductible=3000, review=15000 |
| TAX-02 | Evidence null/false, capital, generic tax, R&D | Decision по priority table; не все категории автоматически deductible |
| TAX-03 | Total reconciliation и source links | Сумма всех decisions равна debit total; rule/source present |
| TAX-04 | Все tax outputs | filing_ready=false, tax_payable/assessable_profit=null, missing inputs nonempty |

Добавить parameterized validation для duplicate ID, invalid dates, amount float/negative/>2 decimal, foreign currency и mismatched invoice fields. При Decimal JSON validation внимательно отличать strict Python input от JSON strings; явный parser денег должен отвергать binary floats. [Pydantic: Decimal types](https://docs.pydantic.dev/latest/api/standard_library_types/).

## Агент и grounding

| ID | Сценарий | Ожидаемый результат |
|---|---|---|
| AGENT-01 | Mock happy path | ≥2 provider turns с tools; все четыре tools реально выполнены |
| AGENT-02 | Unknown tool / invalid JSON / scope mismatch | Typed error, tool не выполняется, деньги не подменены |
| AGENT-03 | Premature final | Missing tools correction, не complete report |
| AGENT-04 | Бесконечные repeats | Budget exhaustion, partial report, exit 3 |
| AGENT-05 | Timeout / auth failure / retry | Одно transient retry; auth без retry; no silent live→mock |
| AGENT-06 | Multiple calls per turn | Все assistant calls и matching tool_call_id сохранены; per-call budgets |
| GROUND-01 | Fabricated/stale fact/result ID | Rejection, repair один раз, затем partial |
| GROUND-02 | Правильный ID, неверный company/period | Rejection |
| GROUND-03 | Existing fact не доказывает выбранный finding | Predicate failure; число само по себе не достаточный grounding |
| GROUND-04 | Свободный amount/prose/неизвестное поле | Schema rejection |
| GROUND-05 | Описание транзакции содержит «ignore instructions» | Не меняет registry/rules/outputs; контент не исполняется |

## Отчёты, архитектура и setup

REPORT-01: report schema validation и exact evidence linkage. REPORT-02: JSON/Markdown показывают одинаковые значения, null не становится нулём, taxes не заявлены вычисленными. REPORT-03: complete требует full scope всех tools + verified synthesis; partial объясняет omissions.

ARCH-01: проверить imports/domain boundaries и typing; не писать тесты, просто зеркалящие список файлов. E2E-01: offline CLI all, output files, exit 0, profile signatures. E2E-02: одинаковый seed → canonical equality (исключить run IDs/timestamps/durations). SETUP-01: clean install без ключа и network-free mock runtime. SETUP-02: Docker build/run smoke в чистом окружении. DEL-01: GitHub готов к сдаче; DEL-02: README содержит tested commands/sequence/actual trace/limitations.

Live smoke отдельный opt-in test, например marker `live`; после запуска сохранить mode/model и факт получения real tool calls. Не фиксировать свободные ответы модели golden-снимком и не объявлять mock доказательством качества live-модели. Raw logs без ключей.

## Definition of Done

Все R01–R18 имеют проверяемые артефакты; обязательные offline tests/lint/type checks и CLI/Docker smoke прошли. Не проставлять done при наличии только spec/fixtures. Ограничения учёта/налога, статус mock/live и невыполненные проверки явно указаны в сдаче.
