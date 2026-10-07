# Детерминированная финансовая логика

Все суммы — Decimal HKD. Round half up до 0.01 только на границе отображения; промежуточные частные не округлять. Ratios/runway в JSON — decimal strings с двумя знаками. Нельзя использовать rounded runway для пороговой классификации: сравнивать исходное частное.

## Баланс и период

Набор покрывает `[dataset.start, dataset.end)`. Для запроса `[start,end)`:

```text
signed(tx) = +amount для credit, -amount для debit
opening_cash(start) = dataset.opening_balance + sum(signed(tx), tx.date < start)
closing_cash(end) = opening_cash(start) + sum(signed(tx), start <= tx.date < end)
end_of_day_cash(d) = opening_cash(start) + sum(signed(tx), start <= tx.date <= d)
```

Начальный баланс относится к началу первого дня, не к первой транзакции. Баланс учитывает ВСЕ movement types: деньги инвестора улучшают cash, но не являются operating revenue. Для кассовых разрывов внутри одного дня используем суммарный end-of-day balance: PDF не даёт intraday времени. Tie-break IDs нужны для сериализации, а не для выдумывания внутридневного дефицита.

Minimum cash сравнивает opening boundary и остатки каждого календарного дня; minimum date — первая дата достижения минимума. Gap — максимальный непрерывный интервал календарных дней с EOD cash < 0, полуоткрытые даты начала/восстановления. Если восстановление не наступило, interval end=end и `open_at_period_end=true`. Не пропускать дни без транзакций. Cash = 0 не отрицательный gap. Отрицательный synthetic balance обозначает дефицит/потребность в финансировании, а не доступный неограниченный банковский овердрафт.

## Runway

Запросы содержат целое число полных календарных месяцев M. Даже месяц без транзакций участвует как нулевой месяц. Взять только `movement_type=operating`; unknown operating payments включаются в cash burn, хотя не объявляются подтверждённым OPEX.

```text
net_burn(month) = operating_debits(month) - operating_credits(month)
average_net_burn = sum(net_burn(month)) / M
average_gross_outflow = sum(operating_debits(month)) / M
runway = closing_cash / average_net_burn, когда cash > 0 и burn > 0
gross_coverage = closing_cash / average_gross_outflow, когда cash > 0 и outflow > 0
last_month_stress_runway = closing_cash / last_month_net_burn,
                          когда cash > 0 и last_month_net_burn > 0
```

Статусы в порядке приоритета:

| Условие | `runway_status` | `runway_months` |
|---|---|---|
| cash ≤ 0 | `cash_depleted` | `0.00` |
| cash > 0, average burn ≤ 0 | `not_burning` | null |
| cash > 0, average burn > 0 | `finite` | вычисленное число |

Никаких Infinity. Gross coverage null при нулевых outflows; при cash ≤ 0 и положительных outflows — `0.00`. Last-month stress null при неположительном burn, иначе max(cash,0)/burn. Это сценарий «последний месяц повторяется», не вероятностный прогноз.

`low_runway` при `finite` и unrounded runway < 3 либо при cash_depleted. Tool возвращает boolean flag, рассчитанный до округления; аналогично `last_month_stress_low_runway` для stress. При null stress flag=false, при cash_depleted flag=true. Общий стабильный месяц не скрывает stress runway после аномалии. Исторический gap показывается независимо от ending cash и average burn: прибыльная Apex всё равно может испытывать дефицит между расходами и оплатой.

Synthesis различает `low_runway` и `stress_low_runway`. Второй вывод опирается на stress runway, closing cash, last-month burn и исходный stress flag. При активном stress flag полный отчёт обязан включать `stress_low_runway`, даже если average runway безопасен; компактный контекст сохраняет его evidence. Это условный сценарий повторения последнего месяца, а не прогноз вероятности дефицита.

Invoice lag: для operating revenue credits с invoice metadata compute `(posted_on - invoice_issued_on).days`, median lag, count и terms distribution. Ссылка на receipts; не заявлять DSO, просрочку непогашенных invoices или gap forecast. Категории `revenue` и `unknown` receipts обе участвуют в operating inflow.

## Категоризация

Confirmed OPEX: operating debits с категориями `rent`, `fuel`, `logistics`, `subcontractors`, `rd_contractors`, `cloud_services`, `other_opex`. Unknown operating debit идёт в `unclassified_operating_debits`. `inventory` — отдельная cash category, не автоматический расход периода. `capital_equipment`, `profits_tax`, financing/investing/transfer — non-OPEX. Для `taxes_other` требуется classification review; в MVP относится к non-OPEX cash, пока назначение налога не подтверждено.

Return OPEX subtotal, unknown subtotal, inventory subtotal и non-OPEX subtotal; все debits распределяются ровно в одну группу. Категория OPEX сама по себе не гарантирует tax deduction. Share = category / known OPEX subtotal × 100; при subtotal=0 share=null. `opex_concentration` при share ≥ 40% (решение MVP). Refunds/contra entries вне текущей модели.

## Аномалии

Объяснимый v1 algorithm, пороги — типизированный `Dataset.anomaly_config` сервера, не модель. Генератор копирует настройки сценария в immutable snapshot; они участвуют в snapshot/result IDs. Старые datasets без этого поля используют defaults ниже. Lookback/count — положительные целые; ratios — конечные Decimal strings с двумя знаками, не меньше `1.00`; deltas — неотрицательные суммы HKD. Невалидные настройки отклоняются.

Defaults: `minimum_baseline_count=5`, `baseline_lookback_months=3`, `transaction_ratio="3.00"`, `transaction_min_delta="10000.00"`, `monthly_jump_ratio="1.50"`, `monthly_jump_min_delta="10000.00"`. Пункты ниже описывают defaults; детектор использует соответствующие поля config.

1. Для каждого месяца и категории operating debits собрать baseline из ВСЕХ транзакций предыдущих календарных месяцев того же snapshot, максимум три месяца. Future leakage запрещён. Baseline внутри месяца неизменен, включая при subperiod query.
2. При ≥ 5 baseline observations определить median `m`. Outlier если amount ≥ 3 × m И amount - m ≥ 10000 HKD. Baseline IDs и candidate ID включаются в evidence. `unknown` не смешивать с известными категориями.
3. Если baseline мал, outlier не утверждать; добавить warning `INSUFFICIENT_BASELINE`. Нулевая MAD не проблема для median ratio метода; этот метод не называется robust z-score.
4. Отдельно сравнить общий operating debit каждого полного месяца с непосредственно предыдущим месяцем snapshot: jump если previous > 0, current ≥ 1.5 × previous и delta ≥ 10000 HKD. Без previous — warning; previous=0/current>0 → `new_spend`, без процентного ratio.
5. Любой operating debit `category=unknown` и `counterparty_country != HK` → `unclassified_cross_border` review finding независимо от статистической величины. Сам cross-border status не свидетельствует о мошенничестве или отсутствии вычета.

Дедуплицировать по `(kind, sorted IDs)`, сортировать kind/date/ID. Outlier и month jump могут сосуществовать и разделять evidence. Statistical signal не доказывает причину списания. Known injection IDs хранятся в test config и не поступают детектору/LLM как готовый ответ.

## Контрольные числа

Малые fixtures в `fixtures/golden` задают арифметику независимо от будущего генератора. Cloud: initial 600000, revenue 150000, operating debits 480000 → closing 270000, average net burn 110000 → runway 2.45, last-month burn 210000 → stress 1.29; аномальный платёж 120000 при baseline median 20000. Apex: initial 30000, outflow 300000, revenue 360000 → closing 90000, gap начинается 20 января и восстанавливается 28 февраля, ending runway `not_burning`. Harbour: outflows 30000, revenue 60000, initial 40000 → closing 70000; из outflows 15000 unknown cross-border, 12000 rent, 3000 profits tax.
