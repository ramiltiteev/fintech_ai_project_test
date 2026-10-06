# План реализации на 3–5 часов

Offline MVP реализован. Фактические проверки и ограничения — в [implementation-status.md](implementation-status.md). Бюджет ниже — исходная оценка, а не измерение затраченного времени. Чистая установка, Docker и live smoke пока не проверены.

| Этап | Базовый бюджет | Результат и exit criterion |
|---|---:|---|
| 1. Контракты и setup | 20 min | Pydantic-модели, pyproject dependencies, src package, lock, typed input validation |
| 2. Данные и финансовые tools | 55 min | Три seeded cohort datasets, баланс/runway/OPEX/anomaly; golden и edge checks проходят |
| 3. Tax tool | 20 min | Versioned ruleset, expense rows/totals/missing inputs; TAX-01..04 проходят |
| 4. Dispatcher и агент | 35 min | Whitelist, mock loop, real tool calls, live adapter, IDs, budgets, grounding rejection |
| 5. Отчёты и CLI | 25 min | all-команда, JSON/Markdown/trace, complete/partial statuses |
| 6. Сдача | 25 min | Unit/E2E, Docker smoke, README с проверенными командами и реальным trace |

Резерв до 120 min: усилить негативные tests/typing, проверить optional live adapter, исправить packaging/container проблемы и пройти clean checkout. Не расходовать резерв на frontend, web API или Kafka deployment.

## Рабочий backlog

- [x] P01: Initialize Python package; dependencies Pydantic v2, optional LiteLLM, dev pytest/Ruff/mypy/jsonschema. Зафиксировать реально проверенные versions.
- [x] P02: Реализовать dataset validation, immutable snapshots и seed. Сохранить scenario configs как входы генератора, golden fixtures как независимые oracle.
- [x] P03: Pure finance functions, все формулы и status cases из financial spec.
- [x] P04: Actual generator 90/120/150 transactions; scenario injection после baseline, проверка count/period/cash signatures.
- [x] P05: Pure expense classification rules; сверка всех debit totals и null tax amounts.
- [x] P06: Четыре named tools, typed results, fact catalogue, registry, schema export/diff.
- [x] P07: Provider protocol + mock + optional live adapter; message association, limits и safe errors.
- [x] P08: Finding predicate validator + controlled templates; rejected synthesis не попадает в report.
- [x] P09: CLI/outputs, summary, all profiles и partial exit code.
- [x] P10: Test suite, lint, typing; доказательные кейсы по testing.md.
- [x] P11: Dockerfile с подходящим Python base, непривилегированным user и offline entrypoint; зависимости pin, `.dockerignore`.
- [ ] P12: Clean checkout/container verification, README ≈ 1 страница, actual trace excerpt, ограничения.
- [x] P13: GitHub delivery checklist готов; публикация и URL — при сдаче с разрешённым доступом.

## Проверки реализации

```bash
python -m pytest
python -m ruff check .
python -m ruff format --check .
python -m mypy src
python -m fintech_copilot demo --company all --provider mock --seed 42 --output artifacts/demo
docker build -t fintech-copilot .
docker run --rm fintech-copilot demo --company all --provider mock --seed 42
```

Команды приложения и offline checks проверены локальным `.venv/bin/python`. Docker-команды выше пока не выполнялись из-за запрета доступа к daemon. Чистая установка/wheel — открытый P12. P11 отмечает создание Dockerfile, а не подтверждённый build. Optional live SDK не установлен; P07 проверен на protocol fixture без обращения к API.

## Готовность к сдаче

- [x] Четыре tools реально вызваны агентом на всех трёх профилях, не только заявлены в README.
- [x] Apex имеет исторический gap + Net 60/90 receipts; Cloud — anomaly + runway < 3; Harbour — frequent transactions + unknown cross-border review.
- [x] Суммы в report/Markdown совпадают с tool facts; negative grounding tests блокируют fake refs.
- [x] Нет live API dependence в mock path; ключи не записываются в report/trace. Container проверяется отдельно в P12.
- [x] Повторный seed даёт тот же canonical результат; независимые golden expectations не изменены ради passing tests.
- [ ] Установку и Docker проверили в чистом окружении; схема, фактический log и Kafka/EDA ограничения есть в README.
- [x] Налоговая таблица помечена как draft, filing_ready=false; тестовый режим явно указан.
