# Фикстуры и ожидаемые результаты

Все контрагенты и суммы искусственные. Файлы созданы для проектирования и будущих tests; это не реальные счета компаний и не журнал запущенного агента.

## Два вида данных

[scenarios.json](scenarios.json) — конфигурация реализованного генератора полноценной когорты: Apex 90, CloudScale 120, Harbour 150 транзакций за `[2026-07-01,2026-10-01)`. Counts включают injected anomalies/tax payments, а не прибавляются после генерации. Monthly totals — target values; seeded вариации должны сохранять acceptance predicates. Не надо уменьшать эффект сценария ради случайности.

При генерации проверить заданное число и totals до/после injection. Cloud base OPEX=300000 + anomaly 180000, inflows=150000, cash=270000, runway=2.45; extra anomaly включена в count. Apex monthly totals с ранними расходами и поздними поступлениями должны реально создавать EOD gap; terms metadata может начинаться до периода coverage. Harbour category shares относятся к operating cash movements, включая inventory/unknown; отдельный tax payment не входит в operating target.

`golden/` — маленькие hand-designed примеры за январь–март 2026. Они намеренно имеют меньше 50 операций: упрощают независимую проверку арифметики и не заменяют требуемую когортную генерацию.

| Dataset | Количество | Проверяемое поведение |
|---|---:|---|
| [Apex](golden/apex-logistics.json) | 8 | Два cash gaps, ending cash 90000, Net 60/90, not_burning |
| [CloudScale](golden/cloudscale-ai.json) | 19 | Closing 270000, runway 2.45, stress 1.29, аномалия 120000 |
| [Harbour](golden/harbour-trade.json) | 10 | Closing 70000, unknown 15000, profits tax 3000 |

[golden/expected.json](golden/expected.json) — независимые expected values. Это не JSON Schema tool result. Tests сравнивают соответствующие fields и predicates, не предполагают наличие всего output в oracle.

[tool-result.example.json](tool-result.example.json) — ожидаемый пример успешного liquidity tool output на Cloud fixture. [synthesis.example.json](synthesis.example.json) — пример model insight, ссылающийся на его реальные fixture fact IDs. Оба иллюстрируют контракт; один liquidity result не удовлетворяет требованию complete report из четырёх tools.

## Ключевые контрольные вычисления

- Apex minimum=-170000 от 20 февраля, gaps `[Jan 20,Feb 28)` и `[Mar 10,Mar 31)`. Mean paid-invoice lag=(60+90)/2=75 дней. Subcontractor OPEX share=80%.
- Cloud 600000+150000−480000=270000; net burn=(480000−150000)/3=110000; runway=270000/110000. Baseline March R&D: девять платежей 20000 и один 40000 → median 20000; candidate 120000, threshold 60000.
- Harbour 40000+60000−30000=70000. Operating outflows=27000; profits tax 3000 исключён из burn. Tax decision totals: potential 12000, non-deductible 3000, review 15000.

Ожидания меняются только при явном изменении спецификации, не при несовпадении будущего бизнес-кода. Generated full cohorts получают отдельные assertions count/shape/behavior; golden snapshots не должны строиться теми же функциями, которые они тестируют.
