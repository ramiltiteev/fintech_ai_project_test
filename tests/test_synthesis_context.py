"""Real tool results, compact context, complete evidence guidance and one-pass repair."""

from __future__ import annotations

import asyncio
import json
import sys
from types import SimpleNamespace

import pytest

from fintech_copilot.domain.models import Scope
from fintech_copilot.orchestration.agent import run_agent
from fintech_copilot.orchestration.context import SYNTHESIS_OPTIONS_PREFIX
from fintech_copilot.orchestration.grounding import options, validate_synthesis
from fintech_copilot.orchestration.providers import Call, LiveProvider, MockProvider, Turn
from fintech_copilot.tools.dispatcher import TOOLS, Dispatcher


def guidance(messages):
    return json.loads(
        next(
            message["content"]
            for message in reversed(messages)
            if message["role"] == "user" and message["content"].startswith(SYNTHESIS_OPTIONS_PREFIX)
        ).split("\n")[1]
    )


@pytest.mark.parametrize("cid", ["apex-logistics", "cloudscale-ai", "harbour-trade"])
def test_compact_context_keeps_evidence_and_full_audit(golden, cid, validator):
    dataset = golden[cid]
    output = asyncio.run(run_agent(dataset, "Synthetic question", MockProvider()))
    assert output.report.status == "complete"
    dispatcher = Dispatcher(dataset)
    expected = tuple(dispatcher.dispatch(name, dataset.scope.model_dump_json()) for name in TOOLS)
    assert output.report.tool_results == expected
    validator("report.schema.json").validate(output.report.model_dump(mode="json"))
    wire = [json.loads(m["content"]) for m in output.messages if m["role"] == "tool"]
    full = {r.tool_result_id: r for r in expected}
    provided = set()
    for result in wire:
        assert result["format"] == "model-tool-v1" and "data" not in result
        facts = {f.fact_id: f for f in full[result["tool_result_id"]].facts}
        for fact in result["facts"]:
            original = facts[fact["fact_id"]]
            assert fact["value"] == original.model_dump(mode="json")["value"]
            assert fact["metric"] == original.metric and fact["unit"] == original.unit
            provided.add((result["tool_result_id"], fact["fact_id"]))
    available = guidance(output.messages)
    assert available["scope"] == dataset.scope.model_dump(mode="json")
    permitted = options(expected)
    for option in available["finding_options"]:
        bundles = option["evidence_bundles"]
        assert bundles == [
            [ref.model_dump(mode="json") for ref in bundle]
            for bundle in permitted[option["finding_code"]]
        ]
        assert all(
            (ref["tool_result_id"], ref["fact_id"]) in provided
            for bundle in bundles
            for ref in bundle
        )
    compact_bytes = sum(len(m["content"]) for m in output.messages if m["role"] == "tool")
    compact_bytes += len(json.dumps(available))
    full_bytes = sum(len(r.model_dump_json()) for r in expected)
    assert compact_bytes < full_bytes * 0.3


def test_model_can_select_other_supported_bundles_and_actions(golden):
    class SelectOther(MockProvider):
        async def complete(self, messages, tools, timeout):
            turn = await super().complete(messages, tools, timeout)
            if turn.calls:
                return turn
            return Turn(
                content=json.dumps(
                    {
                        "insights": [
                            {
                                "finding_code": option["finding_code"],
                                "action_code": option["allowed_actions"][-1],
                                "evidence": option["evidence_bundles"][-1],
                            }
                            for option in reversed(guidance(messages)["finding_options"])
                        ][:6]
                    }
                )
            )

    dataset = golden["cloudscale-ai"]
    output = asyncio.run(run_agent(dataset, "Synthetic question", SelectOther()))
    assert output.report.status == "complete"
    validate_synthesis(output.report.synthesis, output.report.tool_results, dataset)
    assert output.report.synthesis.insights[-1].action_code == "review_operating_spend"


def test_subperiod_does_not_start_synthesis_phase(golden):
    dataset = golden["cloudscale-ai"]

    class Subperiod(MockProvider):
        first = True

        async def complete(self, messages, tools, timeout):
            if self.first:
                self.first = False
                scope = Scope(**{**dataset.scope.model_dump(), "period_end": "2026-08-01"})
                return Turn(calls=(Call("subperiod", TOOLS[0], scope.model_dump_json()),))
            if tools:
                assert not any(
                    m["content"].startswith(SYNTHESIS_OPTIONS_PREFIX)
                    for m in messages
                    if m["role"] == "user"
                )
            else:
                assert guidance(messages)["scope"] == dataset.scope.model_dump(mode="json")
            return await super().complete(messages, tools, timeout)

    output = asyncio.run(run_agent(dataset, "Synthetic question", Subperiod()))
    assert output.report.status == "complete" and len(output.report.tool_results) == 5


def test_one_repair_fixes_all_insights_from_collected_feedback(golden):
    class RepairAll(MockProvider):
        final_turn = 0
        bad = None

        async def complete(self, messages, tools, timeout):
            turn = await super().complete(messages, tools, timeout)
            if turn.calls:
                return turn
            assert not tools
            self.final_turn += 1
            if self.final_turn == 1:
                self.bad = json.loads(turn.content)
                for insight in self.bad["insights"]:
                    insight["evidence"] = insight["evidence"][:1]
                self.bad["insights"][0]["action_code"] = "monitor_cash"
                return Turn(content=json.dumps(self.bad))
            feedback = json.loads(messages[-1]["content"].split("\n")[1])
            allowed = {o["finding_code"]: o for o in guidance(messages)["finding_options"]}
            for error in feedback["validation_errors"]:
                insight = self.bad["insights"][error["path"][1]]
                if error["code"] == "INSUFFICIENT_EVIDENCE":
                    insight["evidence"] = error["expected_evidence"][0]
                elif error["code"] == "INCOMPATIBLE_ACTION":
                    insight["action_code"] = allowed[insight["finding_code"]]["allowed_actions"][0]
                else:
                    raise AssertionError("Unexpected repair failure")
            return Turn(content=json.dumps(self.bad))

    provider = RepairAll()
    output = asyncio.run(run_agent(golden["cloudscale-ai"], "Synthetic question", provider))
    assert output.report.status == "complete" and provider.final_turn == 2
    (rejection,) = [event for event in output.trace if event["event"] == "grounding_rejected"]
    assert rejection["reason_code"] == "MULTIPLE_GROUNDING_ERRORS"
    failures = rejection["validation_errors"]
    assert len(failures) == len(output.report.synthesis.insights) + 1
    assert {e["path"][1] for e in failures} == set(range(len(output.report.synthesis.insights)))
    assert rejection["errors_truncated"] is False


@pytest.mark.parametrize("reject_format", [False, True])
def test_api_json_mode_on_final_and_repair_without_silent_fallback(
    monkeypatch, golden, reject_format
):
    monkeypatch.setenv("LLM_MODEL", "litellm_proxy/synthetic-model")
    monkeypatch.setenv("LITELLM_PROXY_API_BASE", "https://gateway.test/v1")
    monkeypatch.setenv("LITELLM_PROXY_API_KEY", "synthetic-key")
    requests = []
    mock = MockProvider()
    finals = 0

    async def acompletion(**kwargs):
        nonlocal finals
        requests.append(kwargs)
        turn = await mock.complete(kwargs["messages"], kwargs.get("tools", []), kwargs["timeout"])
        if not turn.calls:
            finals += 1
            assert kwargs["response_format"] == {"type": "json_object"}
            assert "tools" not in kwargs and "tool_choice" not in kwargs
            if reject_format:

                class UnsupportedFormat(Exception):
                    status_code = 400

                raise UnsupportedFormat("Synthetic unsupported response_format")
            if finals == 1:
                turn = Turn(content='{"insights":[]}')
        else:
            assert kwargs["tools"] and "response_format" not in kwargs
        return {"choices": [{"message": turn.message()}]}

    monkeypatch.setitem(sys.modules, "litellm", SimpleNamespace(acompletion=acompletion))
    output = asyncio.run(run_agent(golden["cloudscale-ai"], "Synthetic question", LiveProvider()))
    assert finals == (1 if reject_format else 2)
    assert len(requests) == (4 if reject_format else 5)
    assert output.report.status == ("partial" if reject_format else "complete")
    if reject_format:
        assert output.report.partial_reasons == ("PROVIDER_ERROR",)
        assert len(output.report.tool_results) == 4
