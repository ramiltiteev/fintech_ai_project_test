from __future__ import annotations

import asyncio
import json
from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from importlib.resources import files
from time import monotonic
from typing import Any
from uuid import uuid4

from fintech_copilot.domain.models import Dataset
from fintech_copilot.domain.results import TaxResult, ToolResult
from fintech_copilot.tools.dispatcher import TOOLS, Dispatcher, RejectedCall

from .context import model_tool_result, synthesis_options_message
from .diagnostics import synthesis_diagnostic
from .grounding import full_results, validate_synthesis
from .models import Report, Synthesis
from .providers import Provider, ProviderFailure, Turn

LIMITATIONS = (
    "Synthetic transactions and synthetic supporting evidence",
    "Cash basis; no open AP/AR, FX positions or audited accounts",
    "Runway is a retrospective estimate, not a cash forecast",
    "Tax expense review draft; tax and assessable profit are not calculated; human review required",
)


@dataclass(frozen=True)
class Budgets:
    provider_turns: int = 8
    tool_calls: int = 12
    attempt_timeout: float = 90
    deadline: float = 300
    retry_backoff: float = 1
    synthesis_repairs: int = 1


@dataclass
class Run:
    report: Report
    trace: list[dict[str, Any]]
    messages: list[dict[str, Any]]


DEFAULT_BUDGETS = Budgets()


def assemble_report(
    dataset: Dataset,
    provider: Provider,
    run_id: str,
    results: tuple[ToolResult, ...],
    synthesis: Synthesis | None,
    reasons: tuple[str, ...] = (),
) -> Report:
    """Shared assembly after the caller has validated synthesis and evidence."""
    successful = full_results(results, dataset.scope)
    missing = set(TOOLS) - {r.tool_name for r in successful}
    complete = synthesis is not None and not reasons and not missing
    tax = next((r.data for r in successful if isinstance(r, TaxResult)), None)
    fact_map = {
        (f.tool_result_id, f.fact_id): f for r in results if r.status == "ok" for f in r.facts
    }
    return Report(
        schema_version="1.0",
        run_id=run_id,
        generated_at=datetime.now(UTC),
        company=dataset.company,
        **dataset.scope.model_dump(),
        seed=dataset.seed,
        provider_mode=provider.mode,
        model=provider.model,
        status="complete" if complete else "partial",
        partial_reasons=tuple(dict.fromkeys(reasons)),
        facts=tuple(fact_map.values()),
        synthesis=synthesis,
        tax_draft=tax,
        tool_results=results,
        limitations=LIMITATIONS,
    )


async def run_agent(
    dataset: Dataset,
    question: str,
    provider: Provider,
    budgets: Budgets = DEFAULT_BUDGETS,
    trace_sink: Callable[[dict[str, Any]], None] | None = None,
) -> Run:
    started = monotonic()
    run_id = f"run:{uuid4().hex}"
    dispatcher = Dispatcher(dataset)
    resource = files("fintech_copilot.resources")
    definitions = json.loads(resource.joinpath("tools.json").read_text())
    synthesis_schema = json.loads(resource.joinpath("synthesis.schema.json").read_text())
    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": resource.joinpath("agent-system.md").read_text()
            + "\nFinal response JSON schema:\n"
            + json.dumps(synthesis_schema),
        },
        {
            "role": "user",
            "content": json.dumps(
                {
                    "company": dataset.company.model_dump(mode="json"),
                    "scope": dataset.scope.model_dump(mode="json"),
                    "question": question,
                },
                ensure_ascii=False,
            ),
        },
    ]
    trace: list[dict[str, Any]] = []

    def emit(event: str, **details: Any) -> None:
        item = {
            "run_id": run_id,
            "event_index": len(trace),
            "event": event,
            "timestamp": datetime.now(UTC).isoformat(),
            "provider_mode": provider.mode,
            "model": provider.model,
            **details,
        }
        trace.append(item)
        if trace_sink is not None:
            trace_sink(item)

    emit("run_started", snapshot_id=dataset.snapshot_id, company_id=dataset.company.company_id)
    results: dict[str, ToolResult] = {}
    synthesis = None
    reasons: list[str] = []
    call_count = repairs = 0
    seen_call_ids: set[str] = set()
    synthesis_phase = False
    try:
        for turn_index in range(budgets.provider_turns):
            response: Turn | None = None
            for attempt in range(2):
                remaining = budgets.deadline - (monotonic() - started)
                if remaining <= 0:
                    reasons.append("RUN_DEADLINE")
                    break
                timeout = min(budgets.attempt_timeout, remaining)
                try:
                    response = await asyncio.wait_for(
                        provider.complete(
                            messages, [] if synthesis_phase else definitions, timeout
                        ),
                        timeout=timeout,
                    )
                    break
                except TimeoutError:
                    failure = ProviderFailure("PROVIDER_TIMEOUT", transient=True)
                except ProviderFailure as exc:
                    failure = exc
                except Exception:
                    failure = ProviderFailure("PROVIDER_ERROR")
                emit(
                    "provider_turn",
                    turn=turn_index,
                    attempt=attempt,
                    status="error",
                    error_code=failure.code,
                )
                if not failure.transient or attempt == 1:
                    reasons.append(failure.code)
                    break
                remaining = budgets.deadline - (monotonic() - started)
                if remaining <= budgets.retry_backoff:
                    reasons.append("RUN_DEADLINE")
                    break
                await asyncio.sleep(budgets.retry_backoff)
            if response is None:
                break
            emit(
                "provider_turn",
                turn=turn_index,
                status="ok",
                tool_call_count=len(response.calls),
                finish_reason=response.finish_reason,
                usage=asdict(response.usage) if response.usage is not None else None,
            )
            messages.append(response.message())
            if response.calls:
                exceeded = False
                for call in response.calls:
                    call_count += 1
                    if call_count > budgets.tool_calls or monotonic() - started >= budgets.deadline:
                        code = (
                            "TOOL_CALL_LIMIT" if call_count > budgets.tool_calls else "RUN_DEADLINE"
                        )
                        reasons.append(code)
                        exceeded = True
                        output = json.dumps(
                            {
                                "status": "error",
                                "error": {
                                    "code": code,
                                    "message": "Agent budget exhausted",
                                    "retryable": False,
                                },
                            }
                        )
                    elif not call.id or call.id in seen_call_ids:
                        output = RejectedCall(
                            message="Duplicate or empty tool_call_id"
                        ).model_dump_json()
                        emit(
                            "tool_result",
                            tool_call_id=call.id,
                            status="error",
                            error_code="INVALID_ARGUMENTS",
                        )
                    else:
                        seen_call_ids.add(call.id)
                        call_started = monotonic()
                        # Log only scope arguments, never arbitrary provider text.
                        try:
                            raw = json.loads(call.arguments)
                            safe_args = (
                                {k: raw.get(k) for k in type(dataset.scope).model_fields}
                                if isinstance(raw, dict)
                                else None
                            )
                        except (ValueError, TypeError):
                            safe_args = None
                        emit(
                            "tool_call",
                            tool=call.name if call.name in TOOLS else "unknown",
                            tool_call_id=call.id,
                            arguments=safe_args,
                        )
                        result = dispatcher.dispatch(call.name, call.arguments)
                        output = (
                            result.model_dump_json()
                            if isinstance(result, RejectedCall)
                            else model_tool_result(result)
                        )
                        if isinstance(result, RejectedCall):
                            emit(
                                "tool_result",
                                tool_call_id=call.id,
                                status="error",
                                error_code=result.code,
                            )
                        else:
                            results[result.tool_result_id] = result
                            emit(
                                "tool_result",
                                tool=call.name,
                                tool_call_id=call.id,
                                tool_result_id=result.tool_result_id,
                                status=result.status,
                                error_code=result.error.code if result.status == "error" else None,
                                duration_ms=int((monotonic() - call_started) * 1000),
                            )
                    messages.append({"role": "tool", "tool_call_id": call.id, "content": output})
                if exceeded:
                    break
                if len(full_results(tuple(results.values()), dataset.scope)) == len(TOOLS):
                    messages.append(
                        synthesis_options_message(tuple(results.values()), dataset.scope)
                    )
                    synthesis_phase = True
                continue
            missing = set(TOOLS) - {
                r.tool_name for r in full_results(tuple(results.values()), dataset.scope)
            }
            if missing:
                messages.append(
                    {
                        "role": "user",
                        "content": "Missing full-period tools: " + ", ".join(sorted(missing)),
                    }
                )
                continue
            try:
                candidate = Synthesis.model_validate_json(response.content or "")
                validate_synthesis(candidate, tuple(results.values()), dataset)
                synthesis = candidate
                break
            except ValueError as exc:
                diagnostic = synthesis_diagnostic(exc)
                emit(
                    "grounding_rejected",
                    error_code="INVALID_SYNTHESIS",
                    repair_attempt=repairs,
                    **diagnostic,
                )
                if repairs >= budgets.synthesis_repairs:
                    reasons.append("INVALID_SYNTHESIS")
                    break
                repairs += 1
                messages.append(
                    {
                        "role": "user",
                        "content": "Synthesis rejected. Validation feedback:\n"
                        + json.dumps(diagnostic, ensure_ascii=False)
                        + "\nRepair these errors using successful full-period tool facts. "
                        "Return only a JSON object matching the synthesis schema; "
                        "no Markdown, extra fields, numbers or prose.",
                    }
                )
        else:
            reasons.append("TURN_LIMIT")
    except (KeyboardInterrupt, asyncio.CancelledError):
        reasons.append("INTERRUPTED")
    successful = full_results(tuple(results.values()), dataset.scope)
    missing = set(TOOLS) - {r.tool_name for r in successful}
    if missing:
        reasons.append("MISSING_TOOLS:" + ",".join(sorted(missing)))
    if synthesis is None and not reasons:
        reasons.append("MISSING_SYNTHESIS")
    report = assemble_report(
        dataset, provider, run_id, tuple(results.values()), synthesis, tuple(reasons)
    )
    emit(
        "run_finished",
        status=report.status,
        partial_reasons=report.partial_reasons,
        duration_ms=int((monotonic() - started) * 1000),
    )
    return Run(report, trace, messages)
