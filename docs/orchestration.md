# Tool Calling, grounding и журнал агента

## Цикл

1. Application фиксирует профиль, snapshot, период и бизнес-вопрос. System prompt из `prompts/agent-system.md`; four tool definitions из `contracts/tools.json`.
2. Provider получает сообщения и доступные tools. Если возвращает calls, сохранить assistant turn полностью, включая IDs/аргументы; исполнить calls последовательно в полученном порядке.
3. Каждый call проходит whitelist, JSON decoding, schema validation и company/snapshot/period context checks. Затем dispatcher вызывает настоящий domain tool.
4. Передать модели result message с исходным `tool_call_id`. Ошибка становится typed tool error, не выдуманным успешным ответом.
5. Модель выбирает следующий инструмент/уточнение или structured synthesis. Если synthesis получен до четырёх обязательных full-period результатов, отправить список недостающих tools и продолжить в пределах лимита.
6. Synthesis проходит schema, fact provenance, finding predicates и action compatibility checks. Renderer получает только проверенный объект и server-owned tool facts.

Tool selection — действие модели (или scripted mock). Нельзя заранее посчитать все инструменты и назвать последующую суммаризацию многошаговым tool-calling loop. Multiple calls в одном provider turn допустимы, но бюджеты считаются по каждому call.

LiteLLM документирует передачу `tools`, исполнение возвращённых function calls приложением и возврат сообщений с `tool_call_id`. Адаптер использует этот протокол, а SDK остаётся за provider port. [Официальный Function Calling guide](https://docs.litellm.ai/docs/completion/function_call).

## Бюджеты

| Лимит | Значение MVP | Поведение при превышении |
|---|---|---|
| Provider turns (включая repair) | 8 | partial report, `TURN_LIMIT` |
| Tool calls (включая повторные/невалидные) | 12 | partial, `TOOL_CALL_LIMIT` |
| Provider timeout на попытку | 30 s | transient retry, затем partial |
| Общий run deadline | 120 s | не начинать новые вызовы, partial |
| Transient retries | 1 на provider turn, backoff 1 s | authentication/config ошибки не повторять |
| Synthesis repair | 1 | второй invalid synthesis → partial |
| JSON arguments size | 8 KiB/call | `INVALID_ARGUMENTS` |

Повторный идентичный tool call расходует бюджет, но может возвращать cache result. Tool schemas не дают менять threshold/opening cash. После ошибки инструмента попытка исправить args входит в общий бюджет. Deadline учитывает retries/SDK timeout. Keyboard interrupt завершает run и сохраняет доступные артефакты.

## Fact catalogue и валидатор

`fact_id` уникален внутри run и определяется результатом, metric и scope; result ID включает tool/snapshot/args/ruleset version. Пример: `cloud:liquidity:runway_months`. В synthesis ссылка всегда парная: `{tool_result_id,fact_id}`. Знать ID транзакции недостаточно для утверждения о runway.

Validation checks:

- Result exists, status=ok, company/snapshot совпадают с run. Ни одного fabricated/stale ID.
- Fact реально содержится в указанном result; metric соответствует finding и периоду. Полноразмерный insight не подкрепляется subperiod fact без отдельного scope.
- Evidence transaction IDs существуют в dataset и в derivation footprint соответствующего tool. Opening cash — explicit dataset evidence, а не выдуманная транзакция.
- Predicate true по tool data: pre-rounding low-runway flag, gap intervals nonempty, unrounded category share ≥ 40%, anomaly finding exists, unknown cross-border debit exists, tax expense rows awaiting review exist. Общий обязательный статус draft/review_required не равен наличию спорных expense rows. `no_material_findings` допускается только без любого из перечисленных risk predicates.
- Action соответствует finding. Нельзя выводить «платёж незаконен» или «вычет подтверждён» из statistical anomaly/cross-border flags.
- Synthesis не содержит денег, ставок, свободного prose, дополнительных полей или template substitutions. У модели максимум 6 insights; duplications запрещены.

| Finding code | Минимальная evidence | Разрешённый action code |
|---|---|---|
| `low_runway` | runway + closing cash + average burn | `prepare_liquidity_buffer`, `review_operating_spend` |
| `cash_gap` | gap intervals + minimum cash | `review_collection_terms`, `prepare_liquidity_buffer` |
| `opex_concentration` | category share + category total | `review_operating_spend` |
| `cashflow_anomaly` | anomaly ID, amount, baseline/threshold | `verify_payment_documents` |
| `unclassified_cross_border` | unknown cross-border IDs + total | `verify_payment_documents` |
| `tax_review_required` | tax review IDs/totals + rule IDs | `review_tax_treatment` |
| `no_material_findings` | all four successful full-period outputs and absent risk predicates | `monitor_cash` |

Renderer supplies controlled phrases, e.g. «Оценочный runway — {runway_months} мес. по среднему чистому операционному burn; проверить структуру расходов». Подстановки берутся только из facts. Не добавлять estimated savings, fraud likelihood или обещанный будущий баланс.

## Mock и live

Mock возвращает scripted provider turns: liquidity → categorization + anomaly → tax → structured synthesis из фактов, реально полученных в предыдущих tool messages. Сценарные seed/injection labels ему не нужны. Для негативных тестов отдельные scripts: invalid JSON, unknown tool, wrong scope, fabricated fact, premature final, endless loop, timeout.

Live model выбирается env `LLM_MODEL`; проверить native Tool Calling capability и совместимость schemas перед заявлением поддержки. Structured synthesis декодируется/валидируется локально даже при наличии provider structured-output режима. Ни один live-тест не обязателен для offline suite; live smoke запускается явно и фиксируется как реальный либо невыполненный.

## Trace

JSONL events: `run_started`, `provider_turn`, `tool_call`, `tool_result`, `grounding_rejected`, `report_written`, `run_finished`. Поля: run ID, event index, timestamp, provider mode/model, tool/call/result IDs, args, safe status/error, duration. Results/evidence либо включены, либо связаны с записанным report по result ID. Для сдачи README цитирует фактическую trace строку с именем инструмента и параметрами.
