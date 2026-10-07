from __future__ import annotations

import argparse
import asyncio
import json
import sys
from datetime import UTC, datetime
from importlib.resources import files
from pathlib import Path

from fintech_copilot.domain.generator import generate
from fintech_copilot.orchestration.agent import run_agent
from fintech_copilot.orchestration.providers import (
    ConfigurationError,
    LiveProvider,
    MockProvider,
    Provider,
)
from fintech_copilot.render import markdown


async def demo(args: argparse.Namespace) -> int:
    config = json.loads(files("fintech_copilot.resources").joinpath("scenarios.json").read_text())
    profiles = config["profiles"]
    selected = (
        profiles
        if args.company == "all"
        else [p for p in profiles if p["company"]["company_id"] == args.company]
    )
    if not selected:
        raise ConfigurationError("Unknown company")
    # Validate configuration before creating output directories.
    provider: Provider = LiveProvider() if args.provider == "live" else MockProvider()
    if args.seed < 0:
        raise ConfigurationError("Seed must be nonnegative")
    summary = []
    for profile in selected:
        company_id = profile["company"]["company_id"]
        dataset = generate(config, company_id, args.seed)
        directory = Path(args.output) / company_id
        directory.mkdir(parents=True, exist_ok=True)
        directory.joinpath("dataset.json").write_text(dataset.model_dump_json(indent=2) + "\n")
        with directory.joinpath("trace.jsonl").open("w", encoding="utf-8") as trace_file:
            finished_event: dict[str, object] | None = None

            def sink(event: dict[str, object]) -> None:
                nonlocal finished_event
                if event["event"] == "run_finished":
                    finished_event = event
                    return
                trace_file.write(json.dumps(event, ensure_ascii=False) + "\n")
                trace_file.flush()

            run = await run_agent(
                dataset, args.question or profile["business_question"], provider, trace_sink=sink
            )
            directory.joinpath("report.json").write_text(
                run.report.model_dump_json(indent=2) + "\n"
            )
            directory.joinpath("report.md").write_text(markdown(run.report), encoding="utf-8")
            sink(
                {
                    "event": "report_written",
                    "run_id": run.report.run_id,
                    "event_index": len(run.trace) - 1,
                    "timestamp": datetime.now(UTC).isoformat(),
                    "provider_mode": provider.mode,
                    "model": provider.model,
                    "status": run.report.status,
                }
            )
            if finished_event is not None:
                finished_event["event_index"] = len(run.trace)
                finished_event["timestamp"] = datetime.now(UTC).isoformat()
                trace_file.write(json.dumps(finished_event, ensure_ascii=False) + "\n")
        summary.append(
            {
                "company_id": company_id,
                "status": run.report.status,
                "partial_reasons": list(run.report.partial_reasons),
            }
        )
        print(f"{company_id}: {run.report.status} -> {directory}")
        if "INTERRUPTED" in run.report.partial_reasons:
            break
    Path(args.output, "summary.json").write_text(json.dumps(summary, indent=2) + "\n")
    return 0 if all(item["status"] == "complete" for item in summary) else 3


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Synthetic grounded financial analysis")
    sub = parser.add_subparsers(dest="command", required=True)
    command = sub.add_parser("demo")
    command.add_argument(
        "--company",
        default="all",
        choices=["all", "apex-logistics", "cloudscale-ai", "harbour-trade"],
    )
    command.add_argument("--provider", choices=["mock", "live"], default="mock")
    command.add_argument("--seed", type=int, default=42)
    command.add_argument("--output", default="artifacts/demo")
    command.add_argument("--question")
    interactive = sub.add_parser("chat", help="Interactive financial dialogue")
    interactive.add_argument(
        "--company",
        required=True,
        choices=["apex-logistics", "cloudscale-ai", "harbour-trade"],
    )
    interactive.add_argument("--provider", choices=["mock", "live"], default="mock")
    interactive.add_argument("--seed", type=int, default=42)
    interactive.add_argument("--output", default="artifacts/chat")
    interactive.add_argument(
        "--verbose", action="store_true", help="Show tool arguments and diagnostics"
    )
    args = parser.parse_args(argv)
    try:
        if args.command == "chat":
            from fintech_copilot.chat_console import chat

            return asyncio.run(chat(args))
        return asyncio.run(demo(args))
    except (ConfigurationError, ValueError, OSError) as exc:
        # Never echo provider exception bodies or credentials.
        message = (
            str(exc) if isinstance(exc, ConfigurationError) else "Invalid input or output path"
        )
        print(message, file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 3
