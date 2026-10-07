"""Model-authored analysis plans; progress derives only from real tool results."""

from __future__ import annotations

from typing import Annotated, Literal, Self

from pydantic import Field, StringConstraints, model_validator
from pydantic_core import PydanticCustomError

from fintech_copilot.domain.models import Dataset, FrozenModel, Scope
from fintech_copilot.domain.results import Success, ToolName, ToolResult
from fintech_copilot.tools.dispatcher import TOOLS

PLAN_TOOL = "PlanAnalysis"


class AnalysisStep(FrozenModel):
    task: Annotated[str, StringConstraints(strip_whitespace=True, min_length=1, max_length=240)]
    tool: ToolName
    scope: Scope


class AnalysisPlan(FrozenModel):
    mode: Literal["answer", "report"] = "answer"
    steps: Annotated[tuple[AnalysisStep, ...], Field(min_length=1, max_length=8)]

    @model_validator(mode="after")
    def unique_steps(self) -> Self:
        keys = [(step.tool, step.scope) for step in self.steps]
        if len(set(keys)) != len(keys):
            raise PydanticCustomError("duplicate_plan_steps", "Repeated tool/scope in plan")
        return self


def validate_plan(plan: AnalysisPlan, dataset: Dataset, previous: AnalysisPlan | None) -> None:
    for step in plan.steps:
        try:
            dataset.select(step.scope)
        except ValueError:
            raise PydanticCustomError("invalid_plan_scope", "Invalid plan scope") from None
    if previous and (
        plan.mode != previous.mode or plan.steps[: len(previous.steps)] != previous.steps
    ):
        raise PydanticCustomError("plan_preserve_steps", "Do not discard or reorder existing tasks")
    if plan.mode == "report" and {
        step.tool for step in plan.steps if step.scope == dataset.scope
    } != set(TOOLS):
        raise PydanticCustomError(
            "report_plan_requires_tools", "Full report plan requires all full-period tools"
        )


def completed_results(
    plan: AnalysisPlan, results: tuple[ToolResult, ...]
) -> tuple[Success | None, ...]:
    found: dict[tuple[str, Scope], Success] = {}
    for result in results:
        if result.status == "ok":
            scope = Scope.model_validate(result.model_dump(include=set(Scope.model_fields)))
            found[(result.tool_name, scope)] = result
    return tuple(found.get((step.tool, step.scope)) for step in plan.steps)


def plan_state(plan: AnalysisPlan, results: tuple[ToolResult, ...]) -> list[dict[str, object]]:
    return [
        {
            "step": i,
            "tool": step.tool,
            "scope": step.scope.model_dump(mode="json"),
            "status": "complete" if result is not None else "pending",
        }
        for i, (step, result) in enumerate(
            zip(plan.steps, completed_results(plan, results), strict=True)
        )
    ]


def definition() -> dict[str, object]:
    return {
        "type": "function",
        "function": {
            "name": PLAN_TOOL,
            "description": (
                "Plan every part of the user's financial request in its requested order, "
                "before calling financial tools. Each step selects a tool and exact scope. "
                "The plan can be extended, but its existing steps cannot be removed/reordered. "
                "Use report mode with all four full-period tools for a full CFO report."
            ),
            "parameters": AnalysisPlan.model_json_schema(),
        },
    }
