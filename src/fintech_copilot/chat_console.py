"""Interactive terminal transport; financial work lives in orchestration/tools."""

from __future__ import annotations

import argparse
import json
from importlib.resources import files
from pathlib import Path
from typing import Any

from fintech_copilot.domain.generator import generate
from fintech_copilot.domain.models import Dataset
from fintech_copilot.orchestration.chat import (
    MAX_SESSION_TURNS,
    ChatRun,
    ChatSession,
    MockChatProvider,
)
from fintech_copilot.orchestration.providers import ConfigurationError, LiveProvider, Provider
from fintech_copilot.render import chat_text, markdown

HELP = (
    "Можно спросить: «Какой runway?», «Покажи расходы», «Есть аномалии?», "
    "«Что с налоговыми документами?», «Дай полный отчёт», затем уточнить ответ.\n"
    "Команды: /help — помощь, /reset — новый диалог с тем же snapshot, /exit — выход."
)


def render_reply(run: ChatRun, dataset: Dataset) -> str:
    if run.error_code:
        if run.error_code == "INVALID_REPLY":
            rejection = next(
                (e for e in reversed(run.trace) if e["event"] == "grounding_rejected"), {}
            )
            issues = rejection.get("validation_errors", [])
            cause = {
                "answer_forbids_synthesis": "модель добавила структуру отчёта к обычному ответу",
                "answer_requires_references": "модель не указала источники для ответа",
                "duplicate_findings": "модель повторила один и тот же вывод в отчёте",
                "INCOMPLETE_PLAN_ANSWER": "модель не отразила все шаги плана в нужном порядке",
                "UNCITED_NUMBER": "модель написала число без ссылки на факт инструмента",
                "UNKNOWN_FACT_ALIAS": "модель указала неизвестную ссылку на факт инструмента",
                "raw_quantity_in_text": (
                    "модель записала значение в пояснении вместо элемента с источником"
                ),
                "UNSUPPORTED_NARRATIVE": (
                    "пояснения модели не прошли проверку по данным инструментов"
                ),
            }.get(issues[0].get("code") if issues else "")
            if cause is None:
                cause = {
                    "json": "модель вернула некорректный JSON",
                    "schema": "структура ответа модели не соответствует формату чата",
                }.get(rejection.get("stage", ""), "ответ модели не прошёл проверку достоверности")
            retry = (
                " Расчёты сохранены в кеше; можно повторить запрос."
                if any(r.status == "ok" for r in run.tool_results)
                else ""
            )
            return f"Не удалось подготовить ответ: {cause}. Код: INVALID_REPLY.{retry}"
        return f"Запрос не завершён: {run.error_code}. Достоверный ответ не сформирован."
    assert run.reply is not None
    return chat_text(run.reply.text, run.facts)


async def chat(args: argparse.Namespace) -> int:
    if args.seed < 0:
        raise ConfigurationError("Seed must be nonnegative")
    provider: Provider = LiveProvider() if args.provider == "live" else MockChatProvider()
    config = json.loads(files("fintech_copilot.resources").joinpath("scenarios.json").read_text())
    dataset = generate(config, args.company, args.seed)
    session = ChatSession(dataset, provider)
    directory = Path(args.output)
    directory.mkdir(parents=True, exist_ok=True)
    print(f"Компания: {dataset.company.name}.")
    if args.verbose:
        print(f"Snapshot: {dataset.snapshot_id}.")
    print(
        "LIVE: запросы к модели могут быть платными."
        if provider.mode == "live"
        else "MOCK: сценарный эмулятор диалога; финансовые инструменты настоящие."
    )
    print(HELP)
    print(f"Trace: {directory / 'trace.jsonl'}\n")
    exit_code = 0
    with directory.joinpath("trace.jsonl").open("a", encoding="utf-8") as trace_file:

        def sink(event: dict[str, Any]) -> None:
            trace_file.write(json.dumps(event, ensure_ascii=False) + "\n")
            trace_file.flush()
            kind = event["event"]
            if not args.verbose:
                if kind == "plan_updated":
                    labels = {
                        "CalculateLiquidityRunway": "ликвидность",
                        "CategorizeOpex": "расходы",
                        "DetectCashFlowAnomalies": "аномалии",
                        "DraftHKProfitsTax": "налоговые документы",
                    }
                    print(
                        "  План: " + " → ".join(labels[s["tool"]] for s in event["steps"]),
                        flush=True,
                    )
                elif kind == "tool_call" and event["tool"] != "PlanAnalysis":
                    labels = {
                        "CalculateLiquidityRunway": "Смотрю денежные потоки…",
                        "CategorizeOpex": "Разбираю расходы…",
                        "DetectCashFlowAnomalies": "Проверяю аномалии…",
                        "DraftHKProfitsTax": "Проверяю налоговые документы…",
                    }
                    print("  " + labels.get(event["tool"], "Проверяю запрос…"), flush=True)
                return
            if kind == "provider_started":
                print(f"  [{provider.mode}] модель выбирает следующий шаг…", flush=True)
            elif kind == "tool_call":
                print(
                    f"  → {event['tool']} {json.dumps(event['arguments'], ensure_ascii=False)}",
                    flush=True,
                )
            elif kind == "tool_result":
                cache = "; cache hit" if event.get("cache_hit") else ""
                print(f"  ← {event.get('tool', 'tool')}: {event['status']}{cache}", flush=True)
            elif kind == "provider_turn":
                if event["status"] == "error":
                    print(f"  provider error: {event['error_code']}", flush=True)
                elif event.get("usage"):
                    print(f"  usage: {json.dumps(event['usage'])}", flush=True)
            elif kind == "grounding_rejected":
                print(f"  validation: {json.dumps(event, ensure_ascii=False)}", flush=True)

        while True:
            try:
                question = input("Вы > ").strip()
            except (EOFError, KeyboardInterrupt):
                print("\nДиалог завершён.")
                break
            if not question:
                continue
            if question in {"/exit", "/quit"}:
                break
            if question == "/help":
                print(HELP)
                continue
            if question == "/reset":
                session = ChatSession(dataset, provider)
                print("История и кеш очищены; snapshot сохранён.")
                continue
            if question.startswith("/"):
                print("Неизвестная команда. /help — список команд.")
                continue
            if session.turn_count >= MAX_SESSION_TURNS:
                print("Достигнут лимит сообщений. /reset — начать новый диалог.")
                continue
            if len(question) > 4096:
                print("Сообщение слишком длинное; максимум 4096 символов.")
                continue
            run = await session.send(question, sink)
            if run.report is not None or run.facts:
                output = directory / run.run_id.removeprefix("run:")
                output.mkdir(parents=True, exist_ok=True)
                if run.report:
                    assert run.reply is not None
                    output.joinpath("report.json").write_text(
                        run.report.model_dump_json(indent=2) + "\n", encoding="utf-8"
                    )
                    output.joinpath("report.md").write_text(
                        render_reply(run, dataset) + "\n\n---\n\n" + markdown(run.report),
                        encoding="utf-8",
                    )
                    output.joinpath("reply.json").write_text(
                        run.reply.model_dump_json(indent=2) + "\n", encoding="utf-8"
                    )
                else:
                    result_ids = {f.tool_result_id for f in run.facts}
                    output.joinpath("answer.json").write_text(
                        json.dumps(
                            {
                                "run_id": run.run_id,
                                "dataset_scope": dataset.scope.model_dump(mode="json"),
                                "reply": run.reply.model_dump(mode="json") if run.reply else None,
                                "facts": [f.model_dump(mode="json") for f in run.facts],
                                "tool_results": [
                                    r.model_dump(mode="json")
                                    for r in session.results.values()
                                    if r.tool_result_id in result_ids
                                ],
                            },
                            ensure_ascii=False,
                            indent=2,
                        )
                        + "\n",
                        encoding="utf-8",
                    )
                output.joinpath("reply.md").write_text(
                    render_reply(run, dataset) + "\n", encoding="utf-8"
                )
                if args.verbose:
                    print(f"  Сохранено: {output}")
                elif run.report:
                    print(f"  Полный отчёт: {output / 'report.md'}")
            print(f"\nАгент > {render_reply(run, dataset)}\n")
            if run.error_code:
                exit_code = 3
                if run.error_code == "INTERRUPTED":
                    break
    return exit_code
