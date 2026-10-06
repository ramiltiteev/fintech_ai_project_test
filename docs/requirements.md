# Разбор исходного задания

Источник: локальный PDF, одна страница. PDF и извлечённые исходники в `docs/source/` исключены из Git. Документы ниже фиксируют требования для разработки и проверки; дополнительные инженерные решения помечены как решения MVP.

## Матрица требований

| ID | Требование PDF | Артефакт реализации | Приёмка |
|---|---|---|---|
| R01 | Python, бюджет 3–5 часов | CLI и небольшой пакет | E2E-01, план этапов |
| R02 | 50–150 транзакций за 3–6 месяцев | Генератор с seed, три набора | GEN-01..03 |
| R03 | Доходы, аренда, логистика, налоги, подрядчики | Типы движений и категории | OPEX-01, GEN-02 |
| R04 | `CalculateLiquidityRunway` | Детерминированный tool | FIN-01..06 |
| R05 | `CategorizeOpex` | Детерминированный tool | OPEX-01..03 |
| R06 | `DetectCashFlowAnomalies` | Объяснимый детектор | ANOM-01..03 |
| R07 | Многошаговый агент с Function Calling | Provider adapter + agent loop | AGENT-01..06 |
| R08 | Цифры только из tools, ссылки на транзакции | Fact catalogue + grounding validator | GROUND-01..05 |
| R09 | Регуляторный tool по HK Profits Tax/IRD | `DraftHKProfitsTax`, таблица правил | TAX-01..04 |
| R10 | Структурированный JSON/Markdown для CFO | Report assembler + renderer | REPORT-01..03 |
| R11 | Apex Logistics: Net 60/90, топливо, субподрядчики | Сценарий отсрочки поступлений | CASE-APEX |
| R12 | CloudScale AI: R&D/подрядчики, аномалия, runway < 3 | Сценарий повышенного burn | CASE-CLOUD |
| R13 | Harbour Trade: частые движения, неизвестные cross-border | Сценарий ручной проверки | CASE-HARBOUR |
| R14 | GitHub, разделение Domain / Tools / AI Orchestration | Пакет и публичная сдача пользователем | ARCH-01, DEL-01 |
| R15 | Консольное демо / E2E / минимальный HTTP | CLI + E2E для всех профилей | E2E-01..02 |
| R16 | README ≈ 1 страница: sequence, log, ограничения, Kafka/EDA | Короткий README со ссылками | DEL-02 |
| R17 | Docker/placeholder keys, быстрый запуск, unit tests | Dockerfile, `.env.example`, offline mode | SETUP-01..02 |
| R18 | Без реальных персональных данных; эмуляторы допустимы | Синтетика и маркировка режима | GEN-03, AGENT-01 |

Формулировка «Docker/placeholder API ключи» находится в критерии setup; решение MVP — обеспечить оба. Упоминание `Program.cs` не меняет явно заданный Python: выбираем Python CLI.

## Что оптимизируем

| Вес | Фокус | Практическое решение |
|---|---|---|
| 35% | Tool Calling & Grounding | Реальные tools, проверяемые evidence refs, запрет произвольных чисел в synthesis |
| 30% | Backend architecture | Pydantic-контракты и направленные зависимости |
| 20% | Финансовая реалистичность | Opening balance, чистый burn, календарные месяцы, отдельные cash-gap и runway |
| 15% | Testability & Setup | Seed, mock без сети, контрольные примеры, CLI и Docker |

## Границы подготовки и реализации

Подготовка создала спецификации, schemas, prompts, конфигурации и golden-fixtures. Сейчас offline MVP реализован; фактические проверки и открытые setup пункты — в [implementation-status.md](implementation-status.md).

MVP включает четыре tools, три компании, два provider modes (`mock` и `live`), JSON/Markdown и JSONL trace. HTTP/FastAPI, frontend, база данных, Kafka deployment, retrieval, multi-agent и полноценный учёт не требуются. Kafka/EDA описывается как путь развития.

Live adapter должен поддерживать настоящий Tool Calling, но локальная воспроизводимость не зависит от live API. Хотя PDF допускает эмуляторы, mock не доказывает качество выбранной модели: в сдаче нужно честно указать режим и результат live-проверки, если она выполнена.
