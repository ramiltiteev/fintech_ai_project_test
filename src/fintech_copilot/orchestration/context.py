"""Model-facing projections; complete tool results remain in the financial report."""

from __future__ import annotations

import json
from typing import Any

from fintech_copilot.domain.models import Scope
from fintech_copilot.domain.results import ToolResult

from .grounding import ACTIONS, full_results, options

SYNTHESIS_OPTIONS_PREFIX = "Synthesis options:\n"


def model_tool_result(result: ToolResult) -> str:
    if result.status == "error":
        return result.model_dump_json()
    choices = options((result,))
    selected = {ref.fact_id for bundles in choices.values() for bundle in bundles for ref in bundle}
    selected.add(result.facts[0].fact_id)
    # Keep original financial strings and IDs. No calculations or synthetic facts.
    payload = result.model_dump(mode="json", exclude={"data", "facts"})
    payload["format"] = "model-tool-v1"
    payload["facts"] = [
        fact.model_dump(mode="json", include={"fact_id", "metric", "value", "unit"})
        for fact in result.facts
        if fact.fact_id in selected
    ]
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def synthesis_options(results: tuple[ToolResult, ...], scope: Scope) -> dict[str, Any]:
    choices = options(full_results(results, scope))
    return {
        "scope": scope.model_dump(mode="json"),
        "required_findings": ["stress_low_runway"] if "stress_low_runway" in choices else [],
        "finding_options": [
            {
                "finding_code": code,
                "allowed_actions": list(ACTIONS[code]),
                "evidence_bundles": [
                    [ref.model_dump(mode="json") for ref in bundle] for bundle in bundles
                ],
            }
            for code, bundles in choices.items()
        ],
    }


def synthesis_options_message(results: tuple[ToolResult, ...], scope: Scope) -> dict[str, Any]:
    return {
        "role": "user",
        "content": SYNTHESIS_OPTIONS_PREFIX
        + json.dumps(synthesis_options(results, scope), ensure_ascii=False, separators=(",", ":"))
        + "\nSelect and rank up to six unique supported findings, including required_findings. "
        "Copy one complete evidence "
        "bundle and an allowed action for each finding. Return only the synthesis JSON object.",
    }
