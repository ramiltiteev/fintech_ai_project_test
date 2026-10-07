"""Offline scripted replies exercise the real dispatcher, validator and repair loop."""

from __future__ import annotations

import asyncio
import json

import pytest
from pydantic import ValidationError

from fintech_copilot.orchestration.agent import run_agent
from fintech_copilot.orchestration.diagnostics import synthesis_diagnostic
from fintech_copilot.orchestration.grounding import validate_synthesis
from fintech_copilot.orchestration.models import Synthesis
from fintech_copilot.orchestration.providers import MockProvider, Turn

SECRET = "synthetic-secret-do-not-log"


def corrupt(content, mutation):
    if mutation == "invalid_json":
        return f"not JSON {SECRET}"
    if mutation == "fenced_json":
        return "```json\n" + content + "\n```"
    if mutation == "empty_content":
        return None
    raw = json.loads(content)
    first = raw["insights"][0]
    if mutation == "missing_field":
        del first["action_code"]
    elif mutation == "extra_field":
        first[SECRET] = SECRET
    elif mutation == "invalid_enum":
        first["finding_code"] = SECRET
    elif mutation == "duplicate_findings":
        raw["insights"].append(first.copy())
    elif mutation == "wrong_action":
        first["action_code"] = "monitor_cash"
    elif mutation == "unknown_reference":
        first["evidence"][0]["fact_id"] = SECRET
    elif mutation == "duplicate_reference":
        first["evidence"].append(first["evidence"][0].copy())
    elif mutation == "unsupported_finding":
        first["finding_code"] = "no_material_findings"
        first["action_code"] = "monitor_cash"
    elif mutation == "insufficient_evidence":
        first["evidence"] = first["evidence"][:1]
    else:
        raise AssertionError("Unknown test mutation")
    return json.dumps(raw)


@pytest.mark.parametrize(
    "mutation,stage,reason,issue_code,path",
    [
        ("invalid_json", "json", "INVALID_JSON", "json_invalid", []),
        ("fenced_json", "json", "INVALID_JSON", "json_invalid", []),
        ("empty_content", "json", "INVALID_JSON", "json_invalid", []),
        (
            "missing_field",
            "schema",
            "SCHEMA_VALIDATION_FAILED",
            "missing",
            ["insights", 0, "action_code"],
        ),
        (
            "extra_field",
            "schema",
            "SCHEMA_VALIDATION_FAILED",
            "extra_forbidden",
            ["insights", 0, "[extra]"],
        ),
        (
            "invalid_enum",
            "schema",
            "SCHEMA_VALIDATION_FAILED",
            "literal_error",
            ["insights", 0, "finding_code"],
        ),
        ("duplicate_findings", "schema", "SCHEMA_VALIDATION_FAILED", "duplicate_findings", []),
        (
            "wrong_action",
            "grounding",
            "INCOMPATIBLE_ACTION",
            "INCOMPATIBLE_ACTION",
            ["insights", 0, "action_code"],
        ),
        (
            "unknown_reference",
            "grounding",
            "UNKNOWN_FACT_REFERENCE",
            "UNKNOWN_FACT_REFERENCE",
            ["insights", 0, "evidence", 0],
        ),
        (
            "duplicate_reference",
            "grounding",
            "DUPLICATE_REFERENCE",
            "DUPLICATE_REFERENCE",
            ["insights", 0, "evidence", 3],
        ),
        (
            "unsupported_finding",
            "grounding",
            "UNSUPPORTED_FINDING",
            "UNSUPPORTED_FINDING",
            ["insights", 0, "finding_code"],
        ),
        (
            "insufficient_evidence",
            "grounding",
            "INSUFFICIENT_EVIDENCE",
            "INSUFFICIENT_EVIDENCE",
            ["insights", 0, "evidence"],
        ),
    ],
)
def test_diagnostics_reach_trace_and_repair(golden, mutation, stage, reason, issue_code, path):
    class Repairing(MockProvider):
        invalid_sent = False

        async def complete(self, messages, tools, timeout):
            turn = await super().complete(messages, tools, timeout)
            if not turn.calls and not self.invalid_sent:
                self.invalid_sent = True
                return Turn(content=corrupt(turn.content, mutation))
            return turn

    dataset = golden["cloudscale-ai"]
    emitted = []
    output = asyncio.run(
        run_agent(dataset, "Synthetic question", Repairing(), trace_sink=emitted.append)
    )
    assert output.report.status == "complete"
    validate_synthesis(output.report.synthesis, output.report.tool_results, dataset)
    (rejection,) = [event for event in output.trace if event["event"] == "grounding_rejected"]
    assert rejection["stage"] == stage and rejection["reason_code"] == reason
    assert rejection["repair_attempt"] == 0 and rejection["error_code"] == "INVALID_SYNTHESIS"
    (issue,) = rejection["validation_errors"]
    assert issue["code"] == issue_code and issue["path"] == path
    assert rejection in emitted
    (repair,) = [
        message["content"]
        for message in output.messages
        if message["role"] == "user" and message["content"].startswith("Synthesis rejected.")
    ]
    feedback = json.loads(repair.split("\n")[1])
    for key in ["stage", "reason_code", "validation_errors", "errors_truncated"]:
        assert feedback[key] == rejection[key]
    assert SECRET not in repair + json.dumps(output.trace)
    if mutation == "wrong_action":
        assert (
            "Allowed actions: prepare_liquidity_buffer, review_operating_spend" in issue["message"]
        )
    if mutation in {"unknown_reference", "duplicate_reference", "insufficient_evidence"}:
        pairs = {(fact.tool_result_id, fact.fact_id) for fact in output.report.facts}
        for option in issue["expected_evidence"]:
            assert all((ref["tool_result_id"], ref["fact_id"]) in pairs for ref in option)


def test_repeated_bad_reply_is_partial_with_both_diagnostics(golden):
    class Invalid(MockProvider):
        async def complete(self, messages, tools, timeout):
            turn = await super().complete(messages, tools, timeout)
            return turn if turn.calls else Turn(content=corrupt(turn.content, "extra_field"))

    output = asyncio.run(run_agent(golden["cloudscale-ai"], "Synthetic question", Invalid()))
    assert output.report.status == "partial" and output.report.synthesis is None
    assert output.report.partial_reasons == ("INVALID_SYNTHESIS",)
    rejections = [event for event in output.trace if event["event"] == "grounding_rejected"]
    assert [event["repair_attempt"] for event in rejections] == [0, 1]
    assert all(event["stage"] == "schema" for event in rejections)
    assert SECRET not in output.report.model_dump_json() + json.dumps(output.trace)


def test_many_schema_errors_are_bounded_and_unknown_names_redacted():
    with pytest.raises(ValidationError) as raised:
        Synthesis.model_validate({f"{SECRET}-{index}": SECRET for index in range(20)})
    diagnostic = synthesis_diagnostic(raised.value)
    assert len(diagnostic["validation_errors"]) == 8
    assert diagnostic["errors_truncated"] is True
    assert SECRET not in json.dumps(diagnostic)


def test_arbitrary_exception_text_is_not_diagnostic():
    diagnostic = synthesis_diagnostic(ValueError(SECRET))
    assert diagnostic["reason_code"] == "GROUNDING_VALIDATION_FAILED"
    assert SECRET not in json.dumps(diagnostic)


def test_all_unknown_references_reported_without_echoing_input(golden):
    dataset = golden["cloudscale-ai"]
    output = asyncio.run(run_agent(dataset, "Synthetic question", MockProvider()))
    raw = output.report.synthesis.model_dump(mode="json")
    result_id = raw["insights"][0]["evidence"][0]["tool_result_id"]
    raw["insights"][0]["evidence"].extend(
        {"tool_result_id": result_id, "fact_id": f"{SECRET}-{index}"} for index in range(10)
    )
    with pytest.raises(ValueError) as raised:
        validate_synthesis(Synthesis.model_validate(raw), output.report.tool_results, dataset)
    diagnostic = synthesis_diagnostic(raised.value)
    assert diagnostic["reason_code"] == "MULTIPLE_GROUNDING_ERRORS"
    assert len(diagnostic["validation_errors"]) == 10
    assert all(e["code"] == "UNKNOWN_FACT_REFERENCE" for e in diagnostic["validation_errors"])
    assert diagnostic["errors_truncated"] is False
    assert SECRET not in json.dumps(diagnostic)
