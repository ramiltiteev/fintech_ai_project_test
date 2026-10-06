# HK Profits Tax: проект expense review schedule

Проверка источников: 2026-10-06. Объём MVP — классификация synthetic expenses для последующей бухгалтерской проверки. В PDF используется «HK GAAP / IRD compliance»; cash ledger не позволяет подтвердить соответствие стандартам финансовой отчётности.

## Источники и границы

IRD описывает общий принцип связи вычитаемых расходов с получением chargeable profits; ограничения включают частные расходы, capital expenditure и налоги по IRO, с предусмотренными исключениями. Используем это как основу условий, без вывода о праве на вычет только по названию категории. [IRD: Profits Tax](https://www.ird.gov.hk/eng/tax/bus_pft.htm).

Для BIR51 нужны данные финансовых отчётов и налоговые корректировки. Подготовленный здесь частичный cash-based schedule не содержит полного basis period, accrual adjustments и supporting accounts. [IRD: BIR51 notes](https://www.ird.gov.hk/eng/tax/bir51e_notes.htm), [IRD: supporting documents](https://www.ird.gov.hk/e_pfr/).

R&D имеет отдельные критерии и специальные положения; enhanced deduction автоматически не применяется к оплате любого разработчика. [IRD DIPN 55](https://www.ird.gov.hk/eng/pdf/dipn55.pdf). Числа для повышенных вычетов и налоговые ставки не входят в ruleset MVP.

## Ruleset `hk-expense-review-v1`

Порядок правил важен. Обрабатываются ВСЕ debit transactions периода, чтобы totals сверялись с общим cash outflow. Decisions: `potentially_deductible`, `non_deductible`, `review_required`, `outside_expense_scope`.

| Приоритет / ID | Условие | Decision | Обоснование и что проверить |
|---|---|---|---|
| 1 `HK-OUTSIDE-01` | Financing или transfer | outside_expense_scope | Движение капитала/между счетами не является обычным расходом |
| 2 `HK-TAX-01` | `tax_kind=profits_tax` и category=profits_tax | non_deductible | Profits tax по IRO не обычный вычет; source: IRD Profits Tax |
| 3 `HK-CAPITAL-01` | capital_nature=true или capital_equipment/investing | review_required | Не обычный OPEX deduction; проверить специальные allowances, не объявлять всё безусловно недедуктируемым |
| 4 `HK-PURPOSE-01` | business purpose=false или chargeable profits link=false | non_deductible | Synthetic input явно отрицает необходимые условия; source: IRD Profits Tax |
| 5 `HK-UNCLASSIFIED-01` | unknown, taxes_other, tax_kind=unknown либо непоследовательная tax metadata | review_required | Нужны назначение и документы; иностранный платёж не определяет deductible status |
| 6 `HK-SPECIAL-01` | rd_contractors или inventory | review_required | R&D eligibility / capital-versus-revenue; inventory требует учёта запасов/COGS; sources: DIPN 55 / BIR51 notes |
| 7 `HK-OPEX-01` | Обычный OPEX, business purpose=true, chargeable profits link=true, docs=true, capital_nature=false | potentially_deductible | Условия подтверждены в synthetic metadata; source: IRD Profits Tax |
| 8 `HK-REVIEW-01` | Все остальные debit cases | review_required | Недостаточно сведений; source: IRD Profits Tax |

Документы с `documentation_available=false|null` не дают подтвердить вычет; обычный OPEX в таком случае получает review_required. Уверенность LLM не меняет decision. Cross-border SaaS не автоматически offshore/exempt: territorial sourcing и специальные режимы не реализованы.

Output row: transaction ID, amount, cash category, decision, rule ID, reason code, source URLs, missing evidence. Decision totals складываются в total cash debits; potentially deductible total не называется заявленным налоговым вычетом. Potential deductible OPEX, подтверждённо non-deductible и неизвестные расходы показываются отдельно; inventory/capital не теряются в сверке.

`DraftHKProfitsTax` возвращает `status=review_required`, `filing_ready=false`, `assessable_profit=null`, `tax_payable=null`. Missing inputs включают полный basis period, financial statements, reconciliation cash-to-accrual, source-of-profits analysis, allowances/special deductions и human review. Таблица — структурированный драфт налоговой подготовки, а не заполненный BIR51 и не заключение о compliance.

## Версионирование

Rules хранятся вне prompts, rule IDs и verified date присутствуют в trace/отчёте. Tax tool не обращается к интернету во время demo: так результат воспроизводим. Для новых периодов правила и applicability пересматриваются человеком с обновлением ruleset version. Приведённые правила — ограниченная реализация проекта; доказательств полноты налогового законодательства нет.
